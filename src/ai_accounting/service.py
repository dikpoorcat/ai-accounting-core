from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError
from sqlalchemy import delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased

from .ledger import (
    AccountingPeriodError,
    Entry,
    OpenItemPlan,
    account_balance_fen,
    assert_period_open,
    create_open_items,
    create_voucher,
    posting_period_error_code,
)
from .models import (
    Account,
    AnnualBonusUsage,
    AuditLog,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    BusinessEventDependency,
    Counterparty,
    DeferredOutputVatTransfer,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    Invoice,
    LaborServicePerson,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollBatchVersionSequence,
    PayrollContributionActualEvidence,
    PayrollContributionActualItem,
    PayrollContributionActualSet,
    PayrollContributionActualUse,
    PayrollContributionSupplement,
    PayrollContributionSupplementItem,
    PayrollEventLink,
    PayrollFirstWageTaxTreatment,
    PayrollFirstWageTaxTreatmentEvidence,
    PayrollFirstWageTaxTreatmentUse,
    PayrollLine,
    PayrollOpeningState,
    PayrollPolicyVersion,
    PayrollSalaryActualDeductionAllocation,
    PayrollTaxStateSlot,
    PayrollTaxYearGuard,
    PayrollWithholdingEntitlement,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    TaxPeriod,
    TaxPeriodSource,
    Voucher,
    ZeroTaxPeriodConfirmation,
    event_evidence,
)
from .organization_profiles import profile_as_of
from .payroll import (
    AnnualBonusScenarioInput,
    AnnualBonusTaxPolicy,
    AnnualBonusTaxScenario,
    CalculationValidationError,
    ContributionActualOverride,
    ContributionBaseKind,
    ContributionBases,
    ContributionPolicy,
    ContributionRule,
    CumulativeIncomeTaxPolicy,
    CumulativeTaxPeriodInput,
    CumulativeTaxState,
    EmployeeContributionShortfallTreatment,
    ExpiredPolicyError,
    NeedsInformationError,
    RegularPayrollInput,
    RoundingRule,
    YearMonth,
    allocate_contribution_burden,
    apply_contribution_actuals,
    calculate_annual_bonus_scenarios,
    calculate_contributions,
    calculate_cumulative_withholding,
    calculate_regular_payroll,
    select_annual_bonus_tax_method,
)
from .payroll.annual_bonus import AnnualBonusBracket
from .payroll.annual_bonus import AnnualBonusUsage as CalculatorAnnualBonusUsage
from .payroll.income_tax import TaxBracket
from .schemas import (
    DISABLED_EVENT_TYPES,
    INTERNAL_EVENT_TYPES,
    AnnualBonusTaxMethod,
    ConfirmPayrollRequest,
    EventType,
    FinanceResult,
    PayrollBatchKind,
    PayrollPolicyParameters,
    PayrollResult,
    PayrollResultStatus,
    PayrollWageTaxDeclarationState,
    PreviewPayrollRequest,
    RecordEventRequest,
    RecordPayrollContributionSupplementRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollContributionActualRequest,
    RegisterPayrollFirstWageTaxTreatmentRequest,
    RegisterPayrollOpeningStateRequest,
    RegisterPayrollPolicyVersionRequest,
    ResultStatus,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from .tax import active_tax_rule, calculate_tax_period, split_tax_inclusive


class FinanceService:
    DEFERRED_OUTPUT_VAT_RULE_VERSION = "mof-cai-kuai-2016-22-v1"
    DEFERRED_OUTPUT_VAT_RULE_SOURCE_URL = (
        "https://www.mof.gov.cn/gkml/caizhengwengao/2017wg/wg201703/"
        "201707/t20170707_2641107.htm"
    )
    FIRST_WAGE_TAX_TREATMENT_SOURCE_URL = (
        "https://fgk.chinatax.gov.cn/zcfgk/c100012/c5194937/content.html"
    )

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _canonical_payload_hash(payload: dict[str, Any]) -> str:
        """Hash the complete JSON request with a deterministic representation."""
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _preview_request_payload_hash(self, request: PreviewPayrollRequest) -> str:
        return self._canonical_payload_hash(request.model_dump(mode="json"))

    def _request_payload_hash(self, request: Any) -> str:
        """Hash only caller-supplied business facts, before service derivation."""
        return self._canonical_payload_hash(request.model_dump(mode="json"))

    @staticmethod
    def _uses_bank_settlement(request: RecordEventRequest) -> bool:
        if request.event_type is EventType.INTERNAL_TRANSFER:
            return True
        if request.event_type is EventType.EMPLOYEE_REIMBURSEMENT:
            return request.details.paid_now is True
        if request.event_type is EventType.SALARY_PAYMENT:
            return request.amounts.amount_fen != 0
        return request.event_type in {
            EventType.SERVICE_CASH_SALE,
            EventType.CUSTOMER_RECEIPT,
            EventType.CUSTOMER_ADVANCE,
            EventType.CUSTOMER_REFUND,
            EventType.EXPENSE_CASH,
            EventType.EXPENSE_RECOVERY_RECEIVED,
            EventType.SUPPLIER_PAYMENT,
            EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT,
            EventType.OWNER_LOAN_RECEIVED,
            EventType.OWNER_CONTRIBUTION_RECEIVED,
            EventType.OWNER_REPAYMENT,
            EventType.OTHER_INCOME_RECEIVED,
            EventType.BANK_INTEREST_RECEIVED,
            EventType.REFUNDABLE_DEPOSIT_PAID,
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
            EventType.BANK_FEE,
            EventType.TAX_PAYMENT,
            EventType.SOCIAL_INSURANCE_PAYMENT,
            EventType.HOUSING_FUND_PAYMENT,
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
            EventType.CASH_BANK_TRANSFER,
            EventType.PAYMENT_PLATFORM_TRANSFER,
        }

    @classmethod
    def _bank_account_selections(cls, request: RecordEventRequest) -> list[tuple[str, str | None]]:
        if request.event_type is EventType.INTERNAL_TRANSFER:
            return [
                ("source", request.source_bank_account_code),
                ("destination", request.destination_bank_account_code),
            ]
        if cls._uses_bank_settlement(request):
            return [("settlement", request.bank_account_code)]
        return []

    @staticmethod
    def _bank_settlement_date(request: RecordEventRequest) -> date:
        if request.event_type is EventType.INTERNAL_TRANSFER:
            return request.business_dates.business_date
        return request.business_dates.payment_date or request.business_dates.business_date

    def _validate_bank_account(
        self, org_id: uuid.UUID, account_code: str, settlement_date: date
    ) -> Account:
        """Validate one already-configured real bank account without inferring a default."""

        account = self.session.scalar(
            select(Account).where(Account.org_id == org_id, Account.code == account_code)
        )
        if account is None:
            raise ValueError("BANK_ACCOUNT_NOT_CONFIRMED_FOR_RECONCILIATION")
        if (
            account.active is not True
            or account.category != "asset"
            or account.normal_side != "debit"
            or account.requires_bank_reconciliation is not True
            or account.bank_reconciliation_configured_at is None
        ):
            raise ValueError("BANK_ACCOUNT_NOT_CONFIRMED_FOR_RECONCILIATION")
        start_date = account.bank_reconciliation_start_date
        end_date = account.bank_reconciliation_end_date
        if (
            start_date is None
            or settlement_date < start_date
            or (end_date is not None and settlement_date > end_date)
        ):
            raise ValueError("BANK_ACCOUNT_RECONCILIATION_SCOPE_NOT_EFFECTIVE")
        return account

    @staticmethod
    def _bank_reconciliation_scope_is_confirmed(organization: Organization) -> bool:
        return (
            organization.bank_reconciliation_scope_current_action_id is not None
            and organization.bank_reconciliation_scope_confirmed_at is not None
        )

    def _resolve_bank_transaction_references(
        self, org_id: uuid.UUID, references: list[Any]
    ) -> list[BankTransaction]:
        """Resolve references deterministically; ambiguous fingerprints require an id."""

        resolved: list[BankTransaction] = []
        for reference in references:
            if reference.id is not None:
                row = self.session.scalar(
                    select(BankTransaction)
                    .where(
                        BankTransaction.org_id == org_id,
                        BankTransaction.id == reference.id,
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("BANK_TRANSACTION_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
                if reference.fingerprint is not None and row.fingerprint != reference.fingerprint:
                    raise ValueError("BANK_TRANSACTION_REFERENCE_CONFLICT")
            else:
                matches = list(
                    self.session.scalars(
                        select(BankTransaction)
                        .where(
                            BankTransaction.org_id == org_id,
                            BankTransaction.fingerprint == reference.fingerprint,
                        )
                        .order_by(BankTransaction.id)
                        .limit(2)
                        .with_for_update()
                    ).all()
                )
                if not matches:
                    raise ValueError("BANK_TRANSACTION_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
                if len(matches) > 1:
                    raise ValueError("BANK_TRANSACTION_FINGERPRINT_AMBIGUOUS_USE_ID")
                row = matches[0]
            resolved.append(row)
        ids = [row.id for row in resolved]
        if len(ids) != len(set(ids)):
            raise ValueError("DUPLICATE_BANK_TRANSACTION_REFERENCE")
        return resolved

    @staticmethod
    def _database_error_identity(exc: DBAPIError) -> tuple[str | None, str | None, str | None]:
        """Read bounded driver diagnostics without rendering SQL or parameters."""

        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        return (
            getattr(original, "sqlstate", None) or getattr(original, "pgcode", None),
            getattr(diagnostics, "constraint_name", None),
            getattr(diagnostics, "message_primary", None),
        )

    @classmethod
    def _is_tax_period_source_lock_error(cls, exc: DBAPIError) -> bool:
        sqlstate, _constraint_name, primary_message = cls._database_error_identity(exc)
        return sqlstate == "P0001" and primary_message == "TAX_PERIOD_SOURCE_LOCKED"

    @classmethod
    def _accounting_period_database_error_code(cls, exc: DBAPIError) -> str | None:
        sqlstate, _constraint_name, primary_message = cls._database_error_identity(exc)
        if (
            sqlstate == "P0001"
            and isinstance(primary_message, str)
            and primary_message.startswith("ACCOUNTING_PERIOD_")
        ):
            return primary_message
        return None

    @staticmethod
    def _is_round6_final_dependency_error(exc: DBAPIError) -> bool:
        """Classify only the database closure errors that are safe to expose.

        R6 deliberately places the final correction barrier in deferred
        PostgreSQL triggers.  A public write must translate that narrow,
        expected concurrency result into a business rejection, while every
        unrelated database error must remain visible to the caller's normal
        error boundary rather than being mislabeled as a correction conflict.
        """

        rendered = str(exc)
        return any(
            code in rendered
            for code in (
                "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED",
                "R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED",
                "R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED",
                "R7_FINAL_PAYROLL_PROFILE_TAX_DOWNSTREAM_BLOCKED",
                "R7_FINAL_PAYROLL_POLICY_TAX_DOWNSTREAM_BLOCKED",
            )
        )

    def _assert_round6_final_dependency_constraints_now(self) -> None:
        """Evaluate R6 deferred closures inside the public savepoint.

        PostgreSQL would otherwise surface a user-correctable correction race
        from the caller's later ``session.commit()`` as a bare DBAPI error.
        The static constraint list is intentionally narrow: it checks the
        correction/finalization closure after the complete payroll fact graph
        has been written without changing the scheduling of unrelated
        deferred accounting invariants.
        """

        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text(
                "SELECT to_regprocedure("
                "'finance_assert_profile_correction_dependencies(uuid,uuid)') IS NOT NULL"
            )
        )
        # Historical migration fixtures intentionally run the public service
        # against an exact 0006 schema.  Do not issue an R6-only SET
        # CONSTRAINTS statement before 0007 has installed its functions.
        if installed is not True:
            return
        self.session.execute(
            text(
                "SET CONSTRAINTS "
                "payroll_profile_final_dependency_deferred, "
                "payroll_policy_final_dependency_deferred, "
                "payroll_opening_final_dependency_deferred, "
                "final_payroll_dependency_batch_deferred, "
                "final_payroll_dependency_line_deferred, "
                "final_payroll_dependency_tax_slot_deferred IMMEDIATE"
            )
        )

    def _assert_unfinished_payroll_period_constraint_now(self) -> None:
        """Surface the 0012 calculated-batch period guard before API return."""

        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                "WHERE conname = 'unfinished_payroll_period_invariant_deferred')"
            )
        )
        if installed is not True:
            return
        constraint = "unfinished_payroll_period_invariant_deferred"
        self.session.execute(text(f"SET CONSTRAINTS {constraint} IMMEDIATE"))
        self.session.execute(text(f"SET CONSTRAINTS {constraint} DEFERRED"))

    @staticmethod
    def _payroll_policy_values(
        request: RegisterPayrollPolicyVersionRequest,
    ) -> dict[str, Any]:
        """Keep ORM date values native while serializing nested Decimal policy data safely."""
        values = request.model_dump(exclude={"supersedes_policy_version_id"})
        values["supersedes_id"] = request.supersedes_policy_version_id
        parameters = PayrollPolicyParameters.model_validate(request.parameters)
        values["parameters"] = parameters.model_dump(mode="json")
        return values

    def _idempotency_error(
        self,
        existing: BusinessEvent,
        request_payload_hash: str,
        *,
        payroll_envelope: bool = False,
    ) -> str | None:
        if existing.request_payload_hash is None:
            return (
                "PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"
                if payroll_envelope
                else "IDEMPOTENCY_PAYLOAD_UNVERIFIABLE"
            )
        if existing.request_payload_hash != request_payload_hash:
            return (
                "PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"
                if payroll_envelope
                else "IDEMPOTENCY_PAYLOAD_MISMATCH"
            )
        return None

    def _validate_payroll_batch_evidence(
        self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Validate immutable payroll evidence before creating the draft relation."""
        if len(evidence_ids) != len(set(evidence_ids)):
            raise CalculationValidationError(
                "DUPLICATE_PAYROLL_BATCH_EVIDENCE_REFERENCE",
                "each payroll evidence reference may appear only once",
            )
        if not evidence_ids:
            return []
        evidence = self.session.scalars(
            select(Evidence).where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
        ).all()
        if len(evidence) != len(evidence_ids):
            raise CalculationValidationError(
                "PAYROLL_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH",
                "each payroll evidence reference must belong to this organization",
            )
        return list(evidence_ids)

    def _attach_payroll_batch_evidence(
        self, batch: PayrollBatch, evidence_ids: list[uuid.UUID]
    ) -> None:
        for evidence_id in self._validate_payroll_batch_evidence(batch.org_id, evidence_ids):
            self.session.add(
                PayrollBatchEvidence(
                    org_id=batch.org_id,
                    payroll_batch_id=batch.id,
                    evidence_id=evidence_id,
                )
            )

    def _lock_payroll_tax_year(
        self, org_id: uuid.UUID, employee_ids: list[uuid.UUID], tax_year: int
    ) -> None:
        """Create and lock the persistent tax-order domain in a fixed order.

        A ``FOR UPDATE`` over an empty state-slot range does not lock anything.
        The guard row exists specifically to make the first January/March
        confirmations contend too.  Every caller has already entered the same
        transaction that will re-read state, write slots, events and vouchers.
        """

        ordered_employee_ids = sorted(set(employee_ids), key=str)
        if not ordered_employee_ids:
            raise CalculationValidationError(
                "PAYROLL_TAX_GUARD_REQUIRES_EMPLOYEE", "a cumulative payroll has no employee"
            )
        insert_stmt = (
            pg_insert(PayrollTaxYearGuard)
            if self.session.bind and self.session.bind.dialect.name == "postgresql"
            else sqlite_insert(PayrollTaxYearGuard)
        )
        for employee_id in ordered_employee_ids:
            self.session.execute(
                insert_stmt.values(
                    org_id=org_id,
                    employee_id=employee_id,
                    tax_year=tax_year,
                ).on_conflict_do_nothing(index_elements=["org_id", "employee_id", "tax_year"])
            )
        guards = self.session.scalars(
            select(PayrollTaxYearGuard)
            .where(
                PayrollTaxYearGuard.org_id == org_id,
                PayrollTaxYearGuard.employee_id.in_(ordered_employee_ids),
                PayrollTaxYearGuard.tax_year == tax_year,
            )
            .order_by(PayrollTaxYearGuard.employee_id)
            .with_for_update()
        ).all()
        if [guard.employee_id for guard in guards] != ordered_employee_ids:
            raise CalculationValidationError(
                "PAYROLL_TAX_GUARD_NOT_FOUND",
                "a payroll tax-order guard could not be locked",
            )

    @staticmethod
    def _line_uses_cumulative_tax_state(batch: PayrollBatch, line: PayrollLine) -> bool:
        if batch.batch_kind == PayrollBatchKind.REGULAR.value:
            return line.wage_tax_declaration_state == "declared"
        return batch.tax_method == AnnualBonusTaxMethod.COMBINED.value

    def _allocate_payroll_batch_version(
        self, org_id: uuid.UUID, batch_kind: str, payroll_period: str
    ) -> int:
        """Atomically consume one database-owned payroll draft version.

        A sequence row is inserted with ``next_version=2`` for the first draft and
        atomically incremented for every later draft.  This avoids the race inherent
        in reading ``max(version)`` before inserting a new immutable batch.
        """
        values = {
            "org_id": org_id,
            "batch_kind": batch_kind,
            "payroll_period": payroll_period,
            "next_version": 2,
        }
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = (
                pg_insert(PayrollBatchVersionSequence)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["org_id", "batch_kind", "payroll_period"],
                    set_={
                        "next_version": PayrollBatchVersionSequence.next_version + 1,
                    },
                )
                .returning(PayrollBatchVersionSequence.next_version)
            )
            next_version = self.session.scalar(statement)
        elif dialect == "sqlite":
            statement = (
                sqlite_insert(PayrollBatchVersionSequence)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["org_id", "batch_kind", "payroll_period"],
                    set_={
                        "next_version": PayrollBatchVersionSequence.next_version + 1,
                    },
                )
                .returning(PayrollBatchVersionSequence.next_version)
            )
            next_version = self.session.scalar(statement)
        else:  # pragma: no cover - supported deployments use PostgreSQL or SQLite tests.
            sequence = self.session.scalar(
                select(PayrollBatchVersionSequence)
                .where(
                    PayrollBatchVersionSequence.org_id == org_id,
                    PayrollBatchVersionSequence.batch_kind == batch_kind,
                    PayrollBatchVersionSequence.payroll_period == payroll_period,
                )
                .with_for_update()
            )
            if sequence is None:
                sequence = PayrollBatchVersionSequence(**values)
                self.session.add(sequence)
                self.session.flush()
                next_version = sequence.next_version
            else:
                sequence.next_version += 1
                next_version = sequence.next_version
        if next_version is None:  # pragma: no cover - defensive guard for unsupported drivers.
            raise RuntimeError("failed to allocate payroll batch version")
        return int(next_version) - 1

    def record_event(self, request: RecordEventRequest) -> FinanceResult:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"])

        request_payload_hash = self._request_payload_hash(request)
        payroll_payment = self._payroll_payment_categories(request.event_type) is not None
        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if error := self._idempotency_error(
                existing, request_payload_hash, payroll_envelope=payroll_payment
            ):
                return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
            return self._result_for_existing(existing)

        if self._uses_bank_settlement(request) and not self._bank_reconciliation_scope_is_confirmed(
            organization
        ):
            return FinanceResult(
                status=ResultStatus.NEEDS_INFORMATION,
                missing_information=["bank_reconciliation_scope_confirmation"],
                trace=[
                    {
                        "stage": "validation",
                        "status": "needs_information",
                        "code": "BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                    }
                ],
            )

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

        if self._tax_period_source_is_locked(request):
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=["TAX_PERIOD_SOURCE_LOCKED"],
            )

        try:
            with self.session.begin_nested():
                return self._post_event(organization, request)
        except (ValueError, LookupError) as exc:
            # A concurrent payroll writer may win only after this request has
            # taken its open-item locks.  The failed savepoint is clean, so
            # read the idempotency row again before treating this as a local
            # validation failure.  This gives salary and statutory payments
            # the same replay/mismatch semantics as every other payroll write.
            if payroll_payment:
                existing = self.session.scalar(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if error := self._idempotency_error(
                        existing, request_payload_hash, payroll_envelope=True
                    ):
                        return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
                    return self._result_for_existing(existing)
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=[str(exc)],
            )
        except IntegrityError as exc:
            if self._is_tax_period_source_lock_error(exc):
                return self._store_nonposted(
                    request,
                    status=ResultStatus.REJECTED,
                    errors=["TAX_PERIOD_SOURCE_LOCKED"],
                )
            existing = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                if error := self._idempotency_error(
                    existing, request_payload_hash, payroll_envelope=payroll_payment
                ):
                    return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
                return self._result_for_existing(existing)
            if payroll_payment:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
                )
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=["DATABASE_CONSTRAINT_VIOLATION"],
            )
        except OperationalError:
            if payroll_payment:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
                )
            return self._store_nonposted(
                request,
                status=ResultStatus.REJECTED,
                errors=["DATABASE_CONCURRENCY_CONFLICT"],
            )
        except DBAPIError as exc:
            if self._is_tax_period_source_lock_error(exc):
                return self._store_nonposted(
                    request,
                    status=ResultStatus.REJECTED,
                    errors=["TAX_PERIOD_SOURCE_LOCKED"],
                )
            raise

    def _tax_period_source_is_locked(self, request: RecordEventRequest) -> bool:
        """Reject a new taxable source inside an active confirmed snapshot."""

        tax = request.tax_facts
        if tax is None or tax.taxable is not True or tax.tax_due_on_event is not True:
            return False
        taxable_source = request.event_type in {
            EventType.SERVICE_CASH_SALE,
            EventType.SERVICE_CREDIT_SALE,
            EventType.CUSTOMER_ADVANCE,
        }
        if request.event_type == EventType.SERVICE_FULFILLMENT:
            taxable_source = request.details.get("tax_previously_accrued") is False
        if request.event_type == EventType.CUSTOMER_REFUND:
            taxable_source = request.details.get("refund_kind") == "sale_return"
        obligation_date = request.business_dates.tax_obligation_date
        if not taxable_source or obligation_date is None:
            return False
        return self._tax_obligation_date_is_locked(request.org_id, obligation_date)

    def _tax_obligation_date_is_locked(self, org_id: uuid.UUID, obligation_date: date) -> bool:
        """Return whether an active immutable tax snapshot owns this tax date."""

        return (
            self.session.scalar(
                select(
                    exists().where(
                        TaxPeriod.org_id == org_id,
                        TaxPeriod.status == "posted",
                        TaxPeriod.start_date <= obligation_date,
                        TaxPeriod.end_date >= obligation_date,
                    )
                )
            )
            is True
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
        payroll_payment = self._payroll_payment_categories(request.event_type) is not None
        if payroll_payment:
            self._lock_payroll_open_items(request)
        counterparty = self._resolve_counterparty(request)
        deposit_holder = self._resolve_counterparty_reference(
            request.org_id, request.deposit_holder
        )
        if request.event_type in {
            EventType.EMPLOYEE_REIMBURSEMENT,
            EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT,
        } and (counterparty is None or counterparty.kind != "employee"):
            raise ValueError("employee reimbursement requires an employee counterparty")
        if request.event_type is EventType.OTHER_INCOME_RECEIVED and (
            counterparty is None or counterparty.kind != "other"
        ):
            raise ValueError("other income requires an other counterparty")
        if request.event_type in {
            EventType.REFUNDABLE_DEPOSIT_PAID,
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
        } and (counterparty is None or counterparty.kind not in {"supplier", "other"}):
            raise ValueError("refundable deposit requires a supplier or other counterparty")
        if deposit_holder is not None and deposit_holder.kind not in {"supplier", "other"}:
            raise ValueError("a refundable deposit holder must be a supplier or other counterparty")
        facts = request.model_dump(mode="json")
        linked_original = self._validate_business_links(request)
        entries, derived, open_item_type = self._derive_entries(
            request, counterparty, deposit_holder
        )
        facts["derived"] = derived

        trace = [{"stage": "facts_validated", "event_type": request.event_type.value}]
        bank_accounts = [
            {"side": side, "account_code": code}
            for side, code in self._bank_account_selections(request)
        ]
        if bank_accounts:
            trace.append(
                {
                    "stage": "bank_accounts_validated",
                    "settlement_date": self._bank_settlement_date(request).isoformat(),
                    "accounts": bank_accounts,
                }
            )
        if linked_original is not None:
            trace.append(
                {
                    "stage": "business_dependency_validated",
                    "parent_event_id": str(linked_original.id),
                    "dependency_kind": self._business_dependency_kind(request),
                    "amount_fen": self._amount(request),
                }
            )
        rule_version: str | None
        if payroll_payment:
            trace.extend(self._payroll_payment_trace(request, derived))
            rule_version = "payroll-payment"
        else:
            rule_date = (
                request.business_dates.tax_obligation_date or request.business_dates.posting_date
            )
            rule = active_tax_rule(self.session, organization, rule_date)
            trace.append(
                {
                    "stage": "rule_selected",
                    "rule": rule.code,
                    "version": rule.version,
                    "source_url": rule.source_url,
                }
            )
            rule_version = rule.version
        trace.append(
            {
                "stage": "entries_derived",
                "debit_fen": sum(line.debit_fen for line in entries),
                "credit_fen": sum(line.credit_fen for line in entries),
            }
        )
        event = self._new_event(
            request,
            # Every final event follows the same lifecycle.  The PostgreSQL
            # guard verifies its voucher, evidence and normalized links only
            # when this draft is promoted at the end of the transaction.
            "draft",
            trace,
            facts=facts,
            rule_version=rule_version,
        )
        self.session.add(event)
        self.session.flush()
        self._persist_deferred_output_vat_transfers(event, request, derived)
        if linked_original is not None:
            self.session.add(
                BusinessEventDependency(
                    org_id=request.org_id,
                    parent_event_id=linked_original.id,
                    child_event_id=event.id,
                    dependency_kind=self._business_dependency_kind(request),
                    amount_fen=self._amount(request),
                )
            )
            # The dependency is part of the child's canonical fact graph and
            # must exist before its draft -> posted transition is flushed.
            self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        self._create_invoices(event, request)
        if request.event_type == EventType.SALARY_PAYMENT:
            self._record_payroll_withholding_allocations(event, derived)
        self._match_bank_transactions(event, request)
        self._apply_settlements(event, request, counterparty)
        if payroll_payment:
            # The normalized edge validates against the real settlement row,
            # so it is deliberately written after the payment allocation.
            self._persist_payroll_event_link(event, request, derived)
            # PostgreSQL freezes a link as soon as its parent reaches a final
            # state.  Force the new edges out while ``event`` is still draft;
            # ORM unit-of-work ordering must not decide that transition.
            self.session.flush()

        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.business_dates.posting_date,
            description=request.description or request.event_type.value,
            entries=entries,
        )
        # A refund settles the economic relationship; it never changes the
        # original sale or advance into a technical ``reversed`` event.  Only
        # finance_reverse_event may apply the explicit posted -> reversed
        # state transition with a linked reversal voucher.
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
        if request.event_type == EventType.SALARY_PAYMENT:
            create_open_items(
                self.session,
                event=event,
                plans=self._salary_withholding_open_item_plans(event, derived),
            )
        event.status = "posted"
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
            rule_version=rule_version,
            trace=trace,
            data={"derived": derived},
        )

    def _lock_payroll_open_items(self, request: RecordEventRequest) -> None:
        """Lock all payroll open items in stable order before reading balances."""
        open_item_ids = [allocation.open_item_id for allocation in request.allocations]
        if len(open_item_ids) != len(set(open_item_ids)):
            raise ValueError("DUPLICATE_PAYROLL_OPEN_ITEM_ALLOCATION")
        if not open_item_ids:
            return
        locked = self.session.scalars(
            select(OpenItem)
            .where(OpenItem.org_id == request.org_id, OpenItem.id.in_(open_item_ids))
            .order_by(OpenItem.id)
            .with_for_update()
        ).all()
        if len(locked) != len(open_item_ids):
            if request.event_type in {
                EventType.SOCIAL_INSURANCE_PAYMENT,
                EventType.HOUSING_FUND_PAYMENT,
                EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
            }:
                raise ValueError("STATUTORY_PAYMENT_SOURCE_OPEN_ITEM_NOT_FOUND")
            raise ValueError("PAYROLL_OPEN_ITEM_NOT_FOUND")

    def _derive_entries(
        self,
        request: RecordEventRequest,
        counterparty: Counterparty | None,
        deposit_holder: Counterparty | None = None,
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
            entries = [
                Entry(
                    account_code=request.bank_account_code,
                    debit_fen=amount,
                    counterparty_id=cp_id,
                )
                if event_type == EventType.SERVICE_CASH_SALE
                else Entry(
                    account_role="accounts_receivable",
                    debit_fen=amount,
                    counterparty_id=cp_id,
                )
            ]
            entries.append(
                Entry(account_role="service_revenue", credit_fen=net, counterparty_id=cp_id)
            )
            if vat:
                entries.append(
                    Entry(
                        account_role=(
                            "deferred_output_vat"
                            if self._should_defer_output_vat(request)
                            else "vat_payable"
                        ),
                        credit_fen=vat,
                    )
                )
            if event_type == EventType.SERVICE_CREDIT_SALE:
                open_item_type = "receivable"
            derived = self._sales_derived(request, amount, net, vat, taxable)
            if vat:
                derived["vat_recognition"] = (
                    "deferred" if self._should_defer_output_vat(request) else "payable"
                )

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
                    Entry(
                        account_code=request.bank_account_code,
                        debit_fen=amount,
                        counterparty_id=cp_id,
                    ),
                    Entry(account_role="contract_liability", credit_fen=net, counterparty_id=cp_id),
                ]
                if vat:
                    entries.append(Entry(account_role="vat_payable", credit_fen=vat))
                derived = self._sales_derived(request, amount, net, vat, taxable)
            else:
                entries = [
                    Entry(
                        account_code=request.bank_account_code,
                        debit_fen=amount,
                        counterparty_id=cp_id,
                    ),
                    Entry(
                        account_role="contract_liability", credit_fen=amount, counterparty_id=cp_id
                    ),
                ]

        elif event_type == EventType.CUSTOMER_RECEIPT:
            allocated = sum(item.amount_fen for item in request.allocations)
            excess = amount - allocated
            vat_transfer_plans = self._deferred_output_vat_transfer_plans(request)
            vat_transfer_total = sum(plan["amount_fen"] for plan in vat_transfer_plans)
            entries = [
                Entry(
                    account_code=request.bank_account_code, debit_fen=amount, counterparty_id=cp_id
                )
            ]
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
            if vat_transfer_total:
                entries.extend(
                    [
                        Entry(account_role="deferred_output_vat", debit_fen=vat_transfer_total),
                        Entry(account_role="vat_payable", credit_fen=vat_transfer_total),
                    ]
                )
            derived = {
                "allocated_fen": allocated,
                "advance_fen": excess,
                "deferred_output_vat_transfer_fen": vat_transfer_total,
                "deferred_output_vat_transfers": vat_transfer_plans,
            }

        elif event_type == EventType.CUSTOMER_REFUND:
            refund_kind = request.details["refund_kind"]
            if refund_kind == "advance":
                entries = [
                    Entry(
                        account_role="contract_liability", debit_fen=amount, counterparty_id=cp_id
                    ),
                    Entry(
                        account_code=request.bank_account_code,
                        credit_fen=amount,
                        counterparty_id=cp_id,
                    ),
                ]
            else:
                net, vat, _ = self._sales_split(request, amount)
                entries = [
                    Entry(account_role="service_revenue", debit_fen=net, counterparty_id=cp_id),
                    Entry(
                        account_code=request.bank_account_code,
                        credit_fen=amount,
                        counterparty_id=cp_id,
                    ),
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
            expense_entries = (
                [
                    Entry(
                        account_role=expense_role,
                        debit_fen=item.amount_fen,
                        counterparty_id=cp_id,
                        memo=item.label,
                    )
                    for item in request.expense_components
                ]
                if request.expense_components
                else [
                    Entry(
                        account_role=expense_role,
                        debit_fen=amount,
                        counterparty_id=cp_id,
                    )
                ]
            )
            entries = [
                *expense_entries,
                Entry(
                    account_code=request.bank_account_code,
                    credit_fen=amount,
                    counterparty_id=cp_id,
                )
                if event_type == EventType.EXPENSE_CASH
                else Entry(
                    account_role="accounts_payable",
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]
            if event_type == EventType.EXPENSE_PAYABLE:
                open_item_type = "payable"
            derived = {
                "purchase_tax_treatment": "gross_to_expense",
                "expense_fen": amount,
                "expense_components": [
                    item.model_dump(mode="json") for item in request.expense_components
                ],
            }

        elif event_type == EventType.EXPENSE_RECOVERY_RECEIVED:
            entries = [
                Entry(account_code=request.bank_account_code, debit_fen=amount),
                Entry(account_role=request.amounts.expense_account_role, credit_fen=amount),
            ]
            derived = {
                "expense_recovery_kind": request.details.expense_recovery_kind,
                "expense_recovery_fen": amount,
            }

        elif event_type == EventType.SUPPLIER_PAYMENT:
            entries = [
                Entry(account_role="accounts_payable", debit_fen=amount, counterparty_id=cp_id),
                Entry(
                    account_code=request.bank_account_code, credit_fen=amount, counterparty_id=cp_id
                ),
            ]
            derived = {"allocated_fen": sum(item.amount_fen for item in request.allocations)}

        elif event_type == EventType.EMPLOYEE_REIMBURSEMENT:
            paid_now = bool(request.details["paid_now"])
            reimbursement_kind = request.details.reimbursement_kind or "expense"
            if reimbursement_kind == "refundable_deposit":
                if deposit_holder is None:
                    raise ValueError("deposit_holder is required for a refundable deposit")
                debit_entry = Entry(
                    account_role="employee_receivable",
                    debit_fen=amount,
                    counterparty_id=deposit_holder.id,
                )
                derived = {
                    "reimbursement_kind": reimbursement_kind,
                    "refundable_deposit_fen": amount,
                    "deposit_holder_id": str(deposit_holder.id),
                }
            else:
                debit_entry = Entry(
                    account_role=request.amounts.expense_account_role,
                    debit_fen=amount,
                    counterparty_id=cp_id,
                )
                derived = {
                    "reimbursement_kind": reimbursement_kind,
                    "purchase_tax_treatment": "gross_to_expense",
                    "expense_fen": amount,
                }
            entries = [
                debit_entry,
                Entry(
                    account_code=request.bank_account_code if paid_now else None,
                    account_role=None if paid_now else "employee_payable",
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]
            if not paid_now:
                open_item_type = "payable"

        elif event_type == EventType.OWNER_LOAN_RECEIVED:
            entries = [
                Entry(
                    account_code=request.bank_account_code, debit_fen=amount, counterparty_id=cp_id
                ),
                Entry(account_role="owner_payable", credit_fen=amount, counterparty_id=cp_id),
            ]

        elif event_type == EventType.OWNER_CONTRIBUTION_RECEIVED:
            entries = [
                Entry(
                    account_code=request.bank_account_code, debit_fen=amount, counterparty_id=cp_id
                ),
                Entry(account_role="paid_in_capital", credit_fen=amount, counterparty_id=cp_id),
            ]

        elif event_type == EventType.OWNER_REPAYMENT:
            fee_fen = int(request.details.owner_repayment_fee_fen or 0)
            principal_fen = amount - fee_fen
            entries = [
                Entry(
                    account_role="owner_payable",
                    debit_fen=principal_fen,
                    counterparty_id=cp_id,
                ),
            ]
            if fee_fen:
                entries.append(
                    Entry(account_role="general_expense", debit_fen=fee_fen)
                )
            entries.append(
                Entry(
                    account_code=request.bank_account_code,
                    credit_fen=amount,
                    counterparty_id=cp_id,
                )
            )
            derived = {
                "owner_repayment_principal_fen": principal_fen,
                "owner_repayment_fee_fen": fee_fen,
            }

        elif event_type == EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT:
            allocated = sum(item.amount_fen for item in request.allocations)
            entries = [
                Entry(account_role="employee_payable", debit_fen=amount, counterparty_id=cp_id),
                Entry(
                    account_code=request.bank_account_code,
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]
            derived = {"allocated_fen": allocated}

        elif event_type == EventType.OTHER_INCOME_RECEIVED:
            entries = [
                Entry(
                    account_code=request.bank_account_code,
                    debit_fen=amount,
                    counterparty_id=cp_id,
                ),
                # The baseline role predates this public event and maps to the
                # organization's fixed general non-operating-income account.
                Entry(
                    account_role="tax_relief_income",
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]
            derived = {
                "other_income_kind": request.details["other_income_kind"],
                "non_operating_income_fen": amount,
            }

        elif event_type == EventType.BANK_INTEREST_RECEIVED:
            entries = [
                Entry(account_code=request.bank_account_code, debit_fen=amount),
                Entry(account_role="finance_expense", credit_fen=amount),
            ]
            derived = {"bank_interest_income_fen": amount}

        elif event_type == EventType.REFUNDABLE_DEPOSIT_PAID:
            entries = [
                Entry(
                    account_role="employee_receivable",
                    debit_fen=amount,
                    counterparty_id=cp_id,
                ),
                Entry(
                    account_code=request.bank_account_code,
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]
            open_item_type = "receivable"
            derived = {"refundable_deposit_paid_fen": amount}

        elif event_type == EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED:
            entries = [
                Entry(
                    account_code=request.bank_account_code,
                    debit_fen=amount,
                    counterparty_id=cp_id,
                ),
                Entry(
                    account_role="employee_receivable",
                    credit_fen=amount,
                    counterparty_id=cp_id,
                ),
            ]
            derived = {
                "refundable_deposit_return_fen": amount,
                "allocated_fen": sum(item.amount_fen for item in request.allocations),
            }

        elif event_type == EventType.BANK_FEE:
            entries = [
                Entry(account_role="finance_expense", debit_fen=amount),
                Entry(account_code=request.bank_account_code, credit_fen=amount),
            ]

        elif event_type == EventType.INTERNAL_TRANSFER:
            entries = [
                Entry(account_code=request.destination_bank_account_code, debit_fen=amount),
                Entry(account_code=request.source_bank_account_code, credit_fen=amount),
            ]

        elif event_type == EventType.CASH_BANK_TRANSFER:
            if request.direction == "cash_deposit":
                entries = [
                    Entry(account_code=request.bank_account_code, debit_fen=amount),
                    Entry(account_role="cash", credit_fen=amount),
                ]
            else:
                entries = [
                    Entry(account_role="cash", debit_fen=amount),
                    Entry(account_code=request.bank_account_code, credit_fen=amount),
                ]

        elif event_type == EventType.PAYMENT_PLATFORM_TRANSFER:
            if request.direction == "to_platform":
                entries = [
                    Entry(account_role="payment_platform_funds", debit_fen=amount),
                    Entry(account_code=request.bank_account_code, credit_fen=amount),
                ]
            else:
                entries = [
                    Entry(account_code=request.bank_account_code, debit_fen=amount),
                    Entry(account_role="payment_platform_funds", credit_fen=amount),
                ]

        elif event_type == EventType.TAX_PAYMENT:
            tax_role = {
                "vat": "vat_payable",
                "surtax": "surtax_payable",
                "enterprise_income_tax": "enterprise_income_tax_payable",
            }[request.details["tax_type"]]
            entries = [
                Entry(account_role=tax_role, debit_fen=amount),
                Entry(account_code=request.bank_account_code, credit_fen=amount),
            ]

        elif event_type == EventType.SALARY_PAYMENT:
            withholding = self._salary_payment_facts(request)
            entries = [
                Entry(
                    account_role="employee_salary_payable",
                    debit_fen=withholding["gross_salary_fen"],
                ),
            ]
            if amount:
                entries.append(Entry(account_code=request.bank_account_code, credit_fen=amount))
            for role, field_name in (
                ("withheld_employee_social_payable", "employee_social_insurance_fen"),
                ("withheld_employee_housing_fund_payable", "employee_housing_fund_fen"),
                ("individual_income_tax_payable", "individual_income_tax_fen"),
            ):
                if withholding[field_name]:
                    entries.append(Entry(account_role=role, credit_fen=withholding[field_name]))
            for expense_role, deduction_fen in sorted(
                withholding["actual_salary_deduction_by_expense_role"].items()
            ):
                if deduction_fen:
                    entries.append(Entry(account_role=expense_role, credit_fen=deduction_fen))
            derived = {
                "payable_categories": ["salary"],
                "allocated_gross_salary_fen": withholding["gross_salary_fen"],
                "salary_withholding_allocations": withholding["allocations"],
                "employee_social_insurance_fen": withholding["employee_social_insurance_fen"],
                "employee_housing_fund_fen": withholding["employee_housing_fund_fen"],
                "individual_income_tax_fen": withholding["individual_income_tax_fen"],
                "actual_salary_deduction_fen": withholding[
                    "actual_salary_deduction_fen"
                ],
                "actual_salary_deduction_allocations": withholding[
                    "actual_salary_deduction_allocations"
                ],
                "actual_salary_deduction_by_expense_role": withholding[
                    "actual_salary_deduction_by_expense_role"
                ],
                "payroll_batch_id": withholding["payroll_batch_id"],
                "payroll_line_ids": withholding["payroll_line_ids"],
                "withholding_payment_allocations": withholding["withholding_payment_allocations"],
            }

        elif event_type == EventType.SOCIAL_INSURANCE_PAYMENT:
            category_amounts = self._payroll_payment_allocations(
                request, {"employer_social", "withheld_employee_social"}
            )
            late_fee_fen = int(request.details.social_insurance_late_fee_fen or 0)
            entries = [
                Entry(
                    account_role="employer_social_payable",
                    debit_fen=category_amounts["employer_social"],
                )
                for _ in range(category_amounts["employer_social"] > 0)
            ]
            entries.extend(
                Entry(
                    account_role="withheld_employee_social_payable",
                    debit_fen=category_amounts["withheld_employee_social"],
                )
                for _ in range(category_amounts["withheld_employee_social"] > 0)
            )
            if late_fee_fen:
                entries.append(
                    Entry(
                        account_role="social_insurance_late_fee_expense",
                        debit_fen=late_fee_fen,
                    )
                )
            entries.append(Entry(account_code=request.bank_account_code, credit_fen=amount))
            derived = {
                "payable_categories": sorted(category_amounts),
                "allocated_fen": amount - late_fee_fen,
                "social_insurance_late_fee_fen": late_fee_fen,
            }

        elif event_type == EventType.HOUSING_FUND_PAYMENT:
            category_amounts = self._payroll_payment_allocations(
                request, {"employer_housing", "withheld_employee_housing"}
            )
            entries = [
                Entry(
                    account_role="employer_housing_fund_payable",
                    debit_fen=category_amounts["employer_housing"],
                )
                for _ in range(category_amounts["employer_housing"] > 0)
            ]
            entries.extend(
                Entry(
                    account_role="withheld_employee_housing_fund_payable",
                    debit_fen=category_amounts["withheld_employee_housing"],
                )
                for _ in range(category_amounts["withheld_employee_housing"] > 0)
            )
            entries.append(Entry(account_code=request.bank_account_code, credit_fen=amount))
            derived = {"payable_categories": sorted(category_amounts), "allocated_fen": amount}

        elif event_type == EventType.INDIVIDUAL_INCOME_TAX_PAYMENT:
            self._payroll_payment_allocations(request, {"individual_income_tax"})
            entries = [
                Entry(account_role="individual_income_tax_payable", debit_fen=amount),
                Entry(account_code=request.bank_account_code, credit_fen=amount),
            ]
            derived = {"payable_categories": ["individual_income_tax"], "allocated_fen": amount}

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
    def _should_defer_output_vat(request: RecordEventRequest) -> bool:
        """Recognize output VAT later when accounting income precedes the tax date."""

        return bool(
            request.event_type is EventType.SERVICE_CREDIT_SALE
            and request.tax_facts
            and request.tax_facts.taxable
            and request.tax_facts.tax_due_on_event
            and request.business_dates.tax_obligation_date
            and request.business_dates.tax_obligation_date
            > request.business_dates.posting_date
        )

    def _deferred_output_vat_transfer_plans(
        self, request: RecordEventRequest
    ) -> list[dict[str, Any]]:
        """Derive receipt-side VAT transfers solely from normalized receivable sources."""

        if request.event_type is not EventType.CUSTOMER_RECEIPT:
            return []
        payment_date = request.business_dates.payment_date
        if payment_date is None:
            return []

        transfer_event = aliased(BusinessEvent)
        plans: list[dict[str, Any]] = []
        for allocation in request.allocations:
            item = self.session.scalar(
                select(OpenItem)
                .where(
                    OpenItem.org_id == request.org_id,
                    OpenItem.id == allocation.open_item_id,
                )
                .with_for_update()
            )
            if item is None:
                continue
            source = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.id == item.source_event_id,
                    BusinessEvent.status == "posted",
                )
            )
            if (
                source is None
                or source.event_type != EventType.SERVICE_CREDIT_SALE.value
                or source.tax_obligation_date != payment_date
                or source.tax_obligation_date <= source.posting_date
                or source.facts.get("derived", {}).get("vat_recognition") != "deferred"
            ):
                continue
            if request.business_dates.posting_date != source.tax_obligation_date:
                raise ValueError(
                    "DEFERRED_OUTPUT_VAT_TRANSFER_POSTING_DATE_MUST_EQUAL_TAX_OBLIGATION_DATE"
                )
            vat_fen = int(source.facts.get("derived", {}).get("vat_fen", 0))
            if vat_fen <= 0:
                continue
            already_transferred = self.session.scalar(
                select(
                    exists().where(
                        DeferredOutputVatTransfer.org_id == request.org_id,
                        DeferredOutputVatTransfer.source_event_id == source.id,
                        DeferredOutputVatTransfer.transfer_event_id == transfer_event.id,
                        transfer_event.status == "posted",
                    )
                )
            )
            if already_transferred:
                continue
            plans.append(
                {
                    "source_event_id": str(source.id),
                    "source_open_item_id": str(item.id),
                    "amount_fen": vat_fen,
                    "tax_obligation_date": source.tax_obligation_date.isoformat(),
                    "accounting_rule_version": self.DEFERRED_OUTPUT_VAT_RULE_VERSION,
                    "accounting_rule_source_url": self.DEFERRED_OUTPUT_VAT_RULE_SOURCE_URL,
                }
            )
        return plans

    def _persist_deferred_output_vat_transfers(
        self,
        event: BusinessEvent,
        request: RecordEventRequest,
        derived: dict[str, Any],
    ) -> None:
        if request.event_type is not EventType.CUSTOMER_RECEIPT:
            return
        for plan in derived.get("deferred_output_vat_transfers", []):
            self.session.add(
                DeferredOutputVatTransfer(
                    org_id=request.org_id,
                    source_event_id=uuid.UUID(plan["source_event_id"]),
                    source_open_item_id=uuid.UUID(plan["source_open_item_id"]),
                    transfer_event_id=event.id,
                    amount_fen=plan["amount_fen"],
                    tax_obligation_date=date.fromisoformat(plan["tax_obligation_date"]),
                    accounting_rule_version=plan["accounting_rule_version"],
                    accounting_rule_source_url=plan["accounting_rule_source_url"],
                )
            )
        if derived.get("deferred_output_vat_transfers"):
            self.session.flush()

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
        allocation_ids = [item.open_item_id for item in request.allocations]
        if len(allocation_ids) != len(set(allocation_ids)):
            raise ValueError("duplicate open item allocation")
        expected_type = (
            "receivable"
            if request.event_type
            in {
                EventType.CUSTOMER_RECEIPT,
                EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
            }
            else "payable"
        )
        payroll_categories = self._payroll_payment_categories(request.event_type)
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
                if request.event_type in {
                    EventType.SOCIAL_INSURANCE_PAYMENT,
                    EventType.HOUSING_FUND_PAYMENT,
                    EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
                }:
                    raise ValueError("STATUTORY_PAYMENT_SOURCE_OPEN_ITEM_NOT_FOUND")
                raise ValueError(f"open item not found: {allocation.open_item_id}")
            if item.item_type != expected_type or item.status not in {"open", "partial"}:
                raise ValueError(f"open item is not an active {expected_type}: {item.id}")
            if payroll_categories is not None and item.payable_category not in payroll_categories:
                if request.event_type in {
                    EventType.SOCIAL_INSURANCE_PAYMENT,
                    EventType.HOUSING_FUND_PAYMENT,
                    EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
                }:
                    raise ValueError("STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES")
                raise ValueError(
                    f"open item category {item.payable_category!r} is not allowed for "
                    f"{request.event_type.value}"
                )
            if payroll_categories is None and (
                counterparty is None or item.counterparty_id != counterparty.id
            ):
                raise ValueError(f"open item belongs to a different counterparty: {item.id}")
            if request.event_type is EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT:
                source_type = self.session.scalar(
                    select(BusinessEvent.event_type).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.id == item.source_event_id,
                    )
                )
                if source_type not in {
                    EventType.EMPLOYEE_REIMBURSEMENT.value,
                    EventType.FIXED_ASSET_ACQUISITION.value,
                }:
                    raise ValueError(
                        f"open item is not an employee reimbursement payable: {item.id}"
                    )
            if request.event_type is EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED:
                source_type = self.session.scalar(
                    select(BusinessEvent.event_type).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.id == item.source_event_id,
                    )
                )
                if source_type != EventType.REFUNDABLE_DEPOSIT_PAID.value:
                    raise ValueError(f"open item is not a refundable deposit receivable: {item.id}")
            available = item.original_amount_fen - item.settled_amount_fen
            if allocation.amount_fen > available:
                raise ValueError(
                    f"allocation exceeds open amount for {item.id}: "
                    f"available={available}, requested={allocation.amount_fen}"
                )
            item.settled_amount_fen += allocation.amount_fen
            if item.settled_amount_fen == item.original_amount_fen:
                item.status = "settled"
            else:
                item.status = "partial"
            self.session.add(
                Settlement(
                    org_id=request.org_id,
                    open_item_id=item.id,
                    payment_event_id=event.id,
                    amount_fen=allocation.amount_fen,
                )
            )

    def _payroll_payment_allocations(
        self, request: RecordEventRequest, allowed_categories: set[str]
    ) -> dict[str, int]:
        """Validate category-bound payroll payment allocations before deriving entries."""

        if not request.allocations:
            raise ValueError("payroll payment requires allocations")
        expected_allocation_fen = self._amount(request)
        if request.event_type is EventType.SOCIAL_INSURANCE_PAYMENT:
            expected_allocation_fen -= int(
                request.details.social_insurance_late_fee_fen or 0
            )
        if expected_allocation_fen <= 0:
            raise ValueError("social insurance late fee must be less than amount_fen")
        if sum(item.amount_fen for item in request.allocations) != expected_allocation_fen:
            raise ValueError("payroll payment allocations must equal amount_fen")
        totals = {category: 0 for category in allowed_categories}
        for allocation in request.allocations:
            item = self.session.scalar(
                select(OpenItem).where(
                    OpenItem.id == allocation.open_item_id,
                    OpenItem.org_id == request.org_id,
                )
            )
            if item is None:
                if request.event_type in {
                    EventType.SOCIAL_INSURANCE_PAYMENT,
                    EventType.HOUSING_FUND_PAYMENT,
                    EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
                }:
                    raise ValueError("STATUTORY_PAYMENT_SOURCE_OPEN_ITEM_NOT_FOUND")
                raise ValueError(f"open item not found: {allocation.open_item_id}")
            if item.item_type != "payable" or item.status not in {"open", "partial"}:
                raise ValueError(f"open item is not an active payable: {item.id}")
            if item.payable_category not in allowed_categories:
                if request.event_type in {
                    EventType.SOCIAL_INSURANCE_PAYMENT,
                    EventType.HOUSING_FUND_PAYMENT,
                    EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
                }:
                    raise ValueError("STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES")
                raise ValueError(
                    f"open item category {item.payable_category!r} is not allowed for "
                    f"{request.event_type.value}"
                )
            available = item.original_amount_fen - item.settled_amount_fen
            if allocation.amount_fen > available:
                raise ValueError(
                    f"allocation exceeds open amount for {item.id}: "
                    f"available={available}, requested={allocation.amount_fen}"
                )
            totals[item.payable_category] += allocation.amount_fen
        return totals

    def _salary_payment_facts(self, request: RecordEventRequest) -> dict[str, Any]:
        """Validate per-kind deductions against normalized payroll entitlements."""

        if not request.allocations:
            raise ValueError("salary payment requires allocations")
        allocation_by_item = {item.open_item_id: item.amount_fen for item in request.allocations}
        if len(allocation_by_item) != len(request.allocations):
            raise ValueError("salary payment cannot allocate an open item more than once")
        withholding_by_item = {
            item.open_item_id: item for item in request.salary_withholding_allocations
        }
        if len(withholding_by_item) != len(request.salary_withholding_allocations):
            raise ValueError("salary payment cannot state withholdings twice for one open item")
        if set(allocation_by_item) != set(withholding_by_item):
            raise ValueError(
                "salary payment needs explicit withholdings for every salary allocation"
            )
        actual_deduction_by_item = {
            item.open_item_id: item.amount_fen
            for item in request.salary_actual_deduction_allocations
        }
        if len(actual_deduction_by_item) != len(
            request.salary_actual_deduction_allocations
        ):
            raise ValueError(
                "salary payment cannot state actual deductions twice for one open item"
            )
        if not set(actual_deduction_by_item).issubset(allocation_by_item):
            raise ValueError(
                "salary actual deduction must belong to a salary allocation"
            )

        batch: PayrollBatch | None = None
        cash_total = 0
        social_total = 0
        housing_total = 0
        tax_total = 0
        actual_deduction_total = 0
        actual_deduction_by_expense_role: dict[str, int] = {}
        serialised_allocations: list[dict[str, Any]] = []
        withholding_allocations: list[dict[str, Any]] = []
        actual_deduction_allocations: list[dict[str, Any]] = []
        for open_item_id, gross_amount in allocation_by_item.items():
            open_item = self.session.scalar(
                select(OpenItem).where(
                    OpenItem.id == open_item_id,
                    OpenItem.org_id == request.org_id,
                )
            )
            if open_item is None:
                raise ValueError(f"open item not found: {open_item_id}")
            if (
                open_item.item_type != "payable"
                or open_item.status not in {"open", "partial"}
                or open_item.payable_category != "salary"
            ):
                raise ValueError(f"open item is not an active salary payable: {open_item_id}")
            available = open_item.original_amount_fen - open_item.settled_amount_fen
            if gross_amount > available:
                raise ValueError(
                    f"allocation exceeds open amount for {open_item_id}: "
                    f"available={available}, requested={gross_amount}"
                )
            source_link = self.session.scalar(
                select(PayrollEventLink).where(
                    PayrollEventLink.org_id == request.org_id,
                    PayrollEventLink.event_id == open_item.source_event_id,
                    PayrollEventLink.link_kind == "payroll_accrual",
                )
            )
            if source_link is None:
                raise ValueError("salary open item does not originate from a payroll accrual")
            source_batch = self.session.scalar(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == request.org_id,
                    PayrollBatch.id == source_link.payroll_batch_id,
                )
            )
            if source_batch is None:
                raise ValueError("salary payroll origin is not available in this organization")
            if batch is not None and batch.id != source_batch.id:
                raise ValueError("one salary payment must allocate salary from one payroll batch")
            batch = source_batch
            line = self.session.scalar(
                select(PayrollLine)
                .join(Employee, Employee.id == PayrollLine.employee_id)
                .where(
                    PayrollLine.org_id == request.org_id,
                    PayrollLine.payroll_batch_id == source_batch.id,
                    Employee.org_id == request.org_id,
                    Employee.counterparty_id == open_item.counterparty_id,
                )
            )
            if line is None:
                raise ValueError("salary open item has no matching payroll line")
            profile = self.session.scalar(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.org_id == request.org_id,
                    EmployeePayrollProfileVersion.id
                    == line.employee_payroll_profile_version_id,
                    EmployeePayrollProfileVersion.employee_id == line.employee_id,
                )
            )
            if profile is None:
                raise ValueError("salary payroll line has no matching payroll expense profile")
            supplied = withholding_by_item[open_item_id]
            entitlements = self.session.scalars(
                select(PayrollWithholdingEntitlement)
                .where(
                    PayrollWithholdingEntitlement.org_id == request.org_id,
                    PayrollWithholdingEntitlement.payroll_line_id == line.id,
                )
                .order_by(
                    PayrollWithholdingEntitlement.contribution_group,
                    PayrollWithholdingEntitlement.insurance_kind,
                )
                .with_for_update()
            ).all()
            social, social_allocations = self._validated_withholding_components(
                supplied.employee_social_insurance_items,
                entitlements,
                "employee_social_insurance",
                "employee social insurance",
                final_payment=gross_amount == available,
            )
            housing, housing_allocations = self._validated_withholding_components(
                supplied.employee_housing_fund_items,
                entitlements,
                "employee_housing_fund",
                "employee housing fund",
                final_payment=gross_amount == available,
            )
            tax_components, tax_allocations = self._validated_withholding_components(
                {"individual_income_tax": supplied.individual_income_tax_fen},
                entitlements,
                "individual_income_tax",
                "individual income tax",
                final_payment=gross_amount == available,
            )
            tax = sum(tax_components.values())
            withholding_total = sum(social.values()) + sum(housing.values()) + tax
            actual_deduction = int(actual_deduction_by_item.get(open_item_id, 0))
            if withholding_total + actual_deduction > gross_amount:
                raise ValueError(
                    "salary withholdings and actual deduction exceed the allocated gross salary"
                )
            cash_total += gross_amount - withholding_total - actual_deduction
            social_total += sum(social.values())
            housing_total += sum(housing.values())
            tax_total += tax
            actual_deduction_total += actual_deduction
            if actual_deduction:
                actual_deduction_by_expense_role[profile.expense_role] = (
                    actual_deduction_by_expense_role.get(profile.expense_role, 0)
                    + actual_deduction
                )
                actual_deduction_allocations.append(
                    {
                        "open_item_id": str(open_item_id),
                        "payroll_line_id": str(line.id),
                        "amount_fen": actual_deduction,
                        "expense_role": profile.expense_role,
                    }
                )
            serialised_allocations.append(
                {
                    "open_item_id": str(open_item_id),
                    "payroll_line_id": str(line.id),
                    "employee_social_insurance_items": social,
                    "employee_housing_fund_items": housing,
                    "individual_income_tax_fen": tax,
                    "actual_salary_deduction_fen": actual_deduction,
                    "expense_role": profile.expense_role,
                }
            )
            withholding_allocations.extend(
                [*social_allocations, *housing_allocations, *tax_allocations]
            )
        if self._amount(request) == 0 and request.bank_transaction_references:
            raise ValueError("ZERO_CASH_SALARY_PAYMENT_FORBIDS_BANK_TRANSACTIONS")
        if batch is None or cash_total != self._amount(request):
            raise ValueError(
                "salary cash payment must equal gross allocations less explicit "
                "withholdings and actual salary deductions"
            )
        return {
            "payroll_batch_id": str(batch.id),
            "gross_salary_fen": sum(allocation_by_item.values()),
            "employee_social_insurance_fen": social_total,
            "employee_housing_fund_fen": housing_total,
            "individual_income_tax_fen": tax_total,
            "actual_salary_deduction_fen": actual_deduction_total,
            "actual_salary_deduction_by_expense_role": actual_deduction_by_expense_role,
            "actual_salary_deduction_allocations": actual_deduction_allocations,
            "allocations": serialised_allocations,
            "payroll_line_ids": sorted(
                {
                    str(item["payroll_line_id"])
                    for item in [
                        *withholding_allocations,
                        *actual_deduction_allocations,
                    ]
                }
            ),
            "withholding_payment_allocations": withholding_allocations,
        }

    def _validated_withholding_components(
        self,
        supplied: dict[str, int],
        entitlements: list[PayrollWithholdingEntitlement],
        contribution_group: str,
        label: str,
        *,
        final_payment: bool,
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        """Compare each insurance kind to its formal entitlement, not a JSON total."""
        expected = {
            entitlement.insurance_kind: entitlement
            for entitlement in entitlements
            if entitlement.contribution_group == contribution_group
        }
        supplied_values = {code: int(amount) for code, amount in supplied.items()}
        if any(amount for code, amount in supplied_values.items() if code not in expected):
            raise ValueError(f"{label} contains an insurance kind outside the payroll line")
        supplied_amounts = {
            code: amount for code, amount in supplied_values.items() if code in expected
        }
        if not expected and any(supplied_amounts.values()):
            raise ValueError(f"{label} has no payroll-line entitlement")
        entitlement_ids = [item.id for item in expected.values()]
        paid_by_entitlement: dict[uuid.UUID, int] = {}
        if entitlement_ids:
            paid_by_entitlement = {
                entitlement_id: int(amount)
                for entitlement_id, amount in self.session.execute(
                    select(
                        PayrollWithholdingPaymentAllocation.entitlement_id,
                        func.coalesce(func.sum(PayrollWithholdingPaymentAllocation.amount_fen), 0),
                    )
                    .where(
                        PayrollWithholdingPaymentAllocation.org_id == entitlements[0].org_id,
                        PayrollWithholdingPaymentAllocation.entitlement_id.in_(entitlement_ids),
                        PayrollWithholdingPaymentAllocation.reversed.is_(False),
                    )
                    .group_by(PayrollWithholdingPaymentAllocation.entitlement_id)
                ).all()
            }
        persisted: list[dict[str, Any]] = []
        for code, entitlement in expected.items():
            amount = supplied_amounts.get(code, 0)
            paid = paid_by_entitlement.get(entitlement.id, 0)
            if paid + amount > entitlement.amount_fen:
                raise ValueError(f"{label} withholding exceeds the payroll-line entitlement")
            if final_payment and paid + amount != entitlement.amount_fen:
                raise ValueError(
                    "final salary payment must explicitly account for every "
                    "payroll-line withholding"
                )
            if amount:
                persisted.append(
                    {
                        "entitlement_id": str(entitlement.id),
                        "payroll_line_id": str(entitlement.payroll_line_id),
                        "contribution_group": contribution_group,
                        "insurance_kind": code,
                        "amount_fen": amount,
                    }
                )
        return supplied_amounts, persisted

    def _salary_withholding_open_item_plans(
        self, event: BusinessEvent, derived: dict[str, Any]
    ) -> list[OpenItemPlan]:
        batch = self.session.get(PayrollBatch, uuid.UUID(derived["payroll_batch_id"]))
        if batch is None:
            raise ValueError("payroll batch not found for salary withholding")
        targets = self._payment_targets(batch.policy_snapshot.get("parameters", {}))
        plans: list[OpenItemPlan] = []
        for field_name, category, target_key in (
            ("employee_social_insurance_items", "withheld_employee_social", "social_insurance"),
            ("employee_housing_fund_items", "withheld_employee_housing", "housing_fund"),
        ):
            components: dict[str, int] = {}
            for allocation in derived["salary_withholding_allocations"]:
                for code, amount in allocation[field_name].items():
                    components[code] = components.get(code, 0) + int(amount)
            if not components:
                continue
            target = targets.get(target_key)
            if target is None:
                raise ValueError(f"missing statutory payment target for {target_key}")
            agency = self._agency_counterparty(event.org_id, target)
            for insurance_kind, amount in components.items():
                if amount:
                    plans.append(
                        OpenItemPlan(
                            counterparty_id=agency.id,
                            item_type="payable",
                            original_amount_fen=amount,
                            due_date=event.payment_date,
                            payable_category=category,
                            payable_agency_code=target["agency_code"],
                            insurance_kind=insurance_kind,
                        )
                    )
        tax_amount = int(derived["individual_income_tax_fen"])
        if tax_amount:
            target = targets.get("individual_income_tax")
            if target is None:
                raise ValueError("missing statutory payment target for individual_income_tax")
            agency = self._agency_counterparty(event.org_id, target)
            plans.append(
                OpenItemPlan(
                    counterparty_id=agency.id,
                    item_type="payable",
                    original_amount_fen=tax_amount,
                    due_date=event.payment_date,
                    payable_category="individual_income_tax",
                    payable_agency_code=target["agency_code"],
                )
            )
        return plans

    def _record_payroll_withholding_allocations(
        self, event: BusinessEvent, derived: dict[str, Any]
    ) -> None:
        """Persist statutory and actual salary deductions against their payroll lines."""
        for allocation in derived.get("withholding_payment_allocations", []):
            self.session.add(
                PayrollWithholdingPaymentAllocation(
                    org_id=event.org_id,
                    entitlement_id=uuid.UUID(allocation["entitlement_id"]),
                    payment_event_id=event.id,
                    amount_fen=int(allocation["amount_fen"]),
                )
            )
        for allocation in derived.get("actual_salary_deduction_allocations", []):
            self.session.add(
                PayrollSalaryActualDeductionAllocation(
                    org_id=event.org_id,
                    payroll_line_id=uuid.UUID(allocation["payroll_line_id"]),
                    payment_event_id=event.id,
                    amount_fen=int(allocation["amount_fen"]),
                    expense_role=allocation["expense_role"],
                )
            )

    def _persist_payroll_event_link(
        self, event: BusinessEvent, request: RecordEventRequest, derived: dict[str, Any]
    ) -> None:
        """Persist normalized payroll payment provenance.

        A statutory payment may settle several batches, but every source edge
        retains its own source batch and open item.  Compatibility is proven
        from frozen relations before the edges are written; it is never
        inferred from an event facts JSON snapshot.
        """
        if request.event_type == EventType.SALARY_PAYMENT:
            batch_id = uuid.UUID(derived["payroll_batch_id"])
            for allocation in request.allocations:
                self.session.add(
                    PayrollEventLink(
                        org_id=event.org_id,
                        event_id=event.id,
                        payroll_batch_id=batch_id,
                        source_open_item_id=allocation.open_item_id,
                        link_kind="salary_payment",
                    )
                )
            return
        if request.event_type not in {
            EventType.SOCIAL_INSURANCE_PAYMENT,
            EventType.HOUSING_FUND_PAYMENT,
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
        }:
            return
        source_items = self.session.scalars(
            select(OpenItem).where(
                OpenItem.org_id == event.org_id,
                OpenItem.id.in_([allocation.open_item_id for allocation in request.allocations]),
            )
        ).all()
        requested_source_item_ids = {allocation.open_item_id for allocation in request.allocations}
        if len(source_items) != len(requested_source_item_ids):
            raise ValueError("STATUTORY_PAYMENT_SOURCE_OPEN_ITEM_NOT_FOUND")
        source_event_ids = {item.source_event_id for item in source_items}
        links = self.session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.org_id == event.org_id,
                PayrollEventLink.event_id.in_(source_event_ids),
                PayrollEventLink.link_kind.in_(
                    ("payroll_accrual", "salary_payment", "contribution_supplement")
                ),
            )
        ).all()
        links_by_event: dict[uuid.UUID, list[PayrollEventLink]] = {}
        for link in links:
            links_by_event.setdefault(link.event_id, []).append(link)
        supplement_event_ids = {
            link.event_id for link in links if link.link_kind == "contribution_supplement"
        }
        source_link_kinds = {
            item.id: (
                "contribution_supplement"
                if item.source_event_id in supplement_event_ids
                else (
                    "payroll_accrual"
                    if item.payable_category in {"employer_social", "employer_housing"}
                    else "salary_payment"
                )
            )
            for item in source_items
        }

        source_links: dict[uuid.UUID, PayrollEventLink] = {}
        for item in source_items:
            expected_kind = source_link_kinds[item.id]
            candidates = [
                link
                for link in links_by_event.get(item.source_event_id, [])
                if link.link_kind == expected_kind
            ]
            if not candidates:
                raise ValueError("STATUTORY_PAYMENT_SOURCE_IS_NOT_A_LINKED_PAYROLL_EVENT")
            batch_ids_for_item = {link.payroll_batch_id for link in candidates}
            if len(batch_ids_for_item) != 1:
                raise ValueError("STATUTORY_PAYMENT_MIXES_INCOMPATIBLE_PAYROLL_BATCHES")
            if expected_kind == "salary_payment":
                # The source must be a genuine salary payment rather than an
                # arbitrary event labelled as one.  Its own canonical edge
                # must name an open salary item it actually settled.
                salary_source_item_ids = [
                    link.source_open_item_id
                    for link in candidates
                    if link.source_open_item_id is not None
                ]
                if not salary_source_item_ids or not self.session.scalar(
                    select(
                        exists().where(
                            Settlement.org_id == event.org_id,
                            Settlement.payment_event_id == item.source_event_id,
                            Settlement.open_item_id.in_(salary_source_item_ids),
                            Settlement.reversed.is_(False),
                        )
                    )
                ):
                    raise ValueError("STATUTORY_PAYMENT_SOURCE_SALARY_SETTLEMENT_MISSING")
            source_links[item.id] = candidates[0]

        batches_by_id = {
            batch.id: batch
            for batch in self.session.scalars(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == event.org_id,
                    PayrollBatch.id.in_({link.payroll_batch_id for link in source_links.values()}),
                )
            ).all()
        }
        agencies_by_id = {
            agency.id: agency
            for agency in self.session.scalars(
                select(Counterparty).where(
                    Counterparty.org_id == event.org_id,
                    Counterparty.id.in_({item.counterparty_id for item in source_items}),
                )
            ).all()
        }
        compatibility_keys = {
            self._statutory_payment_compatibility_key(
                event=event,
                request=request,
                source_item=item,
                source_link=source_links[item.id],
                source_batch=batches_by_id.get(source_links[item.id].payroll_batch_id),
                source_agency=agencies_by_id.get(item.counterparty_id),
            )
            for item in source_items
        }
        if len(compatibility_keys) != 1:
            raise ValueError("STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES")
        for item in source_items:
            self.session.add(
                PayrollEventLink(
                    org_id=event.org_id,
                    event_id=event.id,
                    payroll_batch_id=source_links[item.id].payroll_batch_id,
                    source_payment_event_id=item.source_event_id,
                    source_open_item_id=item.id,
                    link_kind="statutory_payment",
                )
            )

    @staticmethod
    def _statutory_payment_compatibility_category(event_type: EventType) -> str:
        return {
            EventType.SOCIAL_INSURANCE_PAYMENT: "social_insurance",
            EventType.HOUSING_FUND_PAYMENT: "housing_fund",
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT: "individual_income_tax",
        }[event_type]

    def _statutory_payment_compatibility_key(
        self,
        *,
        event: BusinessEvent,
        request: RecordEventRequest,
        source_item: OpenItem,
        source_link: PayrollEventLink,
        source_batch: PayrollBatch | None,
        source_agency: Counterparty | None,
    ) -> tuple[object, ...]:
        """Return the statutory compatibility key for one canonical source edge.

        The statutory category is the payment family rather than the raw
        employer/employee payable subtype: both social liabilities may be
        settled together, while a social and a housing/tax source cannot.
        """

        if source_batch is None or source_batch.status != "posted":
            raise ValueError("STATUTORY_PAYMENT_SOURCE_BATCH_NOT_FINAL")
        if source_agency is None:
            raise ValueError("STATUTORY_PAYMENT_SOURCE_AGENCY_NOT_FOUND")
        if (
            not source_item.payable_agency_code
            or not source_agency.external_ref
            or source_item.payable_agency_code != source_agency.external_ref
        ):
            raise ValueError("STATUTORY_PAYMENT_SOURCE_AGENCY_MISMATCH")
        category = self._statutory_payment_compatibility_category(request.event_type)
        targets = self._payment_targets(
            source_batch.policy_snapshot.get("parameters", {})
            if isinstance(source_batch.policy_snapshot, dict)
            else {}
        )
        target = targets[category]
        if source_item.payable_agency_code != target["agency_code"]:
            raise ValueError("STATUTORY_PAYMENT_SOURCE_AGENCY_MISMATCH")
        if category == "individual_income_tax":
            # Individual income tax is controlled by the tax-policy FK on
            # the final batch and belongs to its payment tax month.  The
            # JSON income-tax snapshot is explanatory evidence, not the
            # relational identity used to merge formal payments.
            controlling_policy_id = str(source_batch.policy_version_id)
            statutory_period = source_batch.payment_date.strftime("%Y-%m")
        else:
            # Social insurance and housing fund retain their period-end
            # contribution rule and payroll-period contribution month.
            policy_snapshot = source_batch.policy_snapshot.get("contribution_policy")
            if not isinstance(policy_snapshot, dict) or not policy_snapshot.get("id"):
                raise ValueError("STATUTORY_PAYMENT_SOURCE_POLICY_SNAPSHOT_MISSING")
            controlling_policy_id = str(policy_snapshot["id"])
            statutory_period = source_batch.payroll_period
        return (
            event.org_id,
            category,
            source_agency.id,
            source_item.payable_agency_code,
            source_agency.external_ref,
            controlling_policy_id,
            statutory_period,
            request.amounts.currency,
        )

    def _payroll_payment_trace(
        self, request: RecordEventRequest, derived: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Read provenance from normalized links; facts JSON is only an audit snapshot."""
        open_item_ids = [allocation.open_item_id for allocation in request.allocations]
        open_items = self.session.scalars(
            select(OpenItem)
            .where(OpenItem.org_id == request.org_id, OpenItem.id.in_(open_item_ids))
            .order_by(OpenItem.id)
        ).all()
        source_event_ids = {item.source_event_id for item in open_items}
        source_links = self.session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.org_id == request.org_id,
                PayrollEventLink.event_id.in_(source_event_ids),
                PayrollEventLink.link_kind.in_(
                    ("payroll_accrual", "salary_payment", "contribution_supplement")
                ),
            )
        ).all()
        batch_ids = {link.payroll_batch_id for link in source_links}
        source_batches = self.session.scalars(
            select(PayrollBatch).where(
                PayrollBatch.org_id == request.org_id,
                PayrollBatch.id.in_(batch_ids),
            )
        ).all()
        payroll_line_ids = [uuid.UUID(item) for item in derived.get("payroll_line_ids", [])]
        if not payroll_line_ids and source_event_ids:
            payroll_line_ids = self.session.scalars(
                select(PayrollWithholdingEntitlement.payroll_line_id)
                .join(
                    PayrollWithholdingPaymentAllocation,
                    PayrollWithholdingPaymentAllocation.entitlement_id
                    == PayrollWithholdingEntitlement.id,
                )
                .where(
                    PayrollWithholdingEntitlement.org_id == request.org_id,
                    PayrollWithholdingPaymentAllocation.org_id == request.org_id,
                    PayrollWithholdingPaymentAllocation.payment_event_id.in_(source_event_ids),
                    PayrollWithholdingPaymentAllocation.reversed.is_(False),
                )
                .distinct()
            ).all()
        profiles = self.session.scalars(
            select(PayrollLine.employee_payroll_profile_version_id).where(
                PayrollLine.org_id == request.org_id,
                PayrollLine.id.in_(payroll_line_ids),
            )
        ).all()
        return [
            {
                "stage": "payroll_payment_evidence",
                "payroll_batch_ids": [str(batch.id) for batch in source_batches],
                "payroll_policy_version_ids": [
                    str(batch.policy_version_id) for batch in source_batches
                ],
                "employee_profile_version_ids": [str(profile_id) for profile_id in profiles],
                "open_item_categories": sorted(
                    {item.payable_category for item in open_items if item.payable_category}
                ),
                "payment_agency_codes": sorted(
                    {item.payable_agency_code for item in open_items if item.payable_agency_code}
                ),
                "insurance_kinds": sorted(
                    {item.insurance_kind for item in open_items if item.insurance_kind}
                ),
                "source_event_ids": [str(item.source_event_id) for item in open_items],
            }
        ]

    @staticmethod
    def _payroll_payment_categories(event_type: EventType) -> set[str] | None:
        return {
            EventType.SALARY_PAYMENT: {"salary"},
            EventType.SOCIAL_INSURANCE_PAYMENT: {
                "employer_social",
                "withheld_employee_social",
            },
            EventType.HOUSING_FUND_PAYMENT: {
                "employer_housing",
                "withheld_employee_housing",
            },
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT: {"individual_income_tax"},
        }.get(event_type)

    def _attach_evidence(
        self,
        event: BusinessEvent,
        evidence_ids: list[uuid.UUID],
        *,
        relation_kind: str = "supporting",
    ) -> None:
        """Attach organization-bound evidence without bypassing edge metadata.

        ``event_evidence`` became a first-class, organization-scoped relation in
        R4.  SQLAlchemy's many-to-many convenience append cannot supply its
        required ``org_id`` and provenance role, so every service write goes
        through this explicit insert path instead.
        """

        if not evidence_ids:
            return
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate event evidence references are not allowed")
        evidence = self.session.scalars(
            select(Evidence).where(Evidence.org_id == event.org_id, Evidence.id.in_(evidence_ids))
        ).all()
        if len(evidence) != len(set(evidence_ids)):
            raise ValueError("one or more evidence references do not exist in this organization")
        self.session.execute(
            event_evidence.insert(),
            [
                {
                    "org_id": event.org_id,
                    "event_id": event.id,
                    "evidence_id": evidence_id,
                    "relation_kind": relation_kind,
                }
                for evidence_id in evidence_ids
            ],
        )

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
        matched = self._resolve_bank_transaction_references(
            request.org_id, request.bank_transaction_references
        )
        for transaction in matched:
            active_match = self.session.scalar(
                select(BankTransactionMatch)
                .where(
                    BankTransactionMatch.org_id == request.org_id,
                    BankTransactionMatch.bank_transaction_id == transaction.id,
                    BankTransactionMatch.invalidated_by_event_id.is_(None),
                )
                .with_for_update()
            )
            if active_match is not None and active_match.event_id != event.id:
                raise ValueError("BANK_TRANSACTION_ALREADY_MATCHED")
            # The legacy pointer remains a fast current-state projection.  It
            # must never override an existing immutable match edge, but retain
            # the same stable rejection for pre-0004 rows in SQLite tests.
            if (
                active_match is None
                and transaction.matched_event_id is not None
                and transaction.matched_event_id != event.id
            ):
                raise ValueError("BANK_TRANSACTION_ALREADY_MATCHED")
        if not matched:
            return

        inflows = {
            EventType.SERVICE_CASH_SALE,
            EventType.CUSTOMER_RECEIPT,
            EventType.CUSTOMER_ADVANCE,
            EventType.OWNER_LOAN_RECEIVED,
            EventType.OWNER_CONTRIBUTION_RECEIVED,
            EventType.OTHER_INCOME_RECEIVED,
            EventType.BANK_INTEREST_RECEIVED,
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
            EventType.EXPENSE_RECOVERY_RECEIVED,
        }
        outflows = {
            EventType.CUSTOMER_REFUND,
            EventType.EXPENSE_CASH,
            EventType.SUPPLIER_PAYMENT,
            EventType.EMPLOYEE_REIMBURSEMENT,
            EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT,
            EventType.OWNER_REPAYMENT,
            EventType.BANK_FEE,
            EventType.REFUNDABLE_DEPOSIT_PAID,
            EventType.TAX_PAYMENT,
            EventType.SALARY_PAYMENT,
            EventType.SOCIAL_INSURANCE_PAYMENT,
            EventType.HOUSING_FUND_PAYMENT,
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
        }
        amount = self._amount(request)
        if request.event_type == EventType.INTERNAL_TRANSFER:
            source_code = request.source_bank_account_code
            destination_code = request.destination_bank_account_code
            if any(
                transaction.bank_account_code not in {source_code, destination_code}
                for transaction in matched
            ):
                raise ValueError("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
            source_total = sum(
                transaction.amount_fen
                for transaction in matched
                if transaction.bank_account_code == source_code
            )
            destination_total = sum(
                transaction.amount_fen
                for transaction in matched
                if transaction.bank_account_code == destination_code
            )
            if source_total != -amount or destination_total != amount:
                raise ValueError("INTERNAL_TRANSFER_BANK_TRANSACTION_AMOUNT_MISMATCH")
        elif request.event_type == EventType.CASH_BANK_TRANSFER:
            if any(
                transaction.bank_account_code != request.bank_account_code
                for transaction in matched
            ):
                raise ValueError("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
            bank_total = sum(transaction.amount_fen for transaction in matched)
            expected = amount if request.direction == "cash_deposit" else -amount
            if bank_total != expected:
                raise ValueError("CASH_BANK_TRANSFER_BANK_TRANSACTION_AMOUNT_MISMATCH")
        elif request.event_type == EventType.PAYMENT_PLATFORM_TRANSFER:
            if any(
                transaction.bank_account_code != request.bank_account_code
                for transaction in matched
            ):
                raise ValueError("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
            bank_total = sum(transaction.amount_fen for transaction in matched)
            expected = amount if request.direction == "from_platform" else -amount
            if bank_total != expected:
                raise ValueError("PAYMENT_PLATFORM_TRANSFER_BANK_TRANSACTION_AMOUNT_MISMATCH")
        else:
            if any(
                transaction.bank_account_code != request.bank_account_code
                for transaction in matched
            ):
                raise ValueError("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
            bank_total = sum(transaction.amount_fen for transaction in matched)
        if request.event_type in inflows and bank_total != amount:
            raise ValueError(
                f"bank inflow total does not match event amount: bank={bank_total}, event={amount}"
            )
        if request.event_type in outflows and bank_total != -amount:
            raise ValueError(
                f"bank outflow total does not match event amount: bank={bank_total}, event={amount}"
            )
        if (
            request.event_type
            not in {
                EventType.INTERNAL_TRANSFER,
                EventType.CASH_BANK_TRANSFER,
                EventType.PAYMENT_PLATFORM_TRANSFER,
            }
            and request.event_type not in inflows | outflows
        ):
            raise ValueError("this event type must not match bank transactions")
        for transaction in matched:
            self.session.add(
                BankTransactionMatch(
                    org_id=event.org_id,
                    bank_transaction_id=transaction.id,
                    event_id=event.id,
                )
            )
            transaction.matched_event_id = event.id

    def _resolve_counterparty_reference(
        self, org_id: uuid.UUID, reference: Any | None
    ) -> Counterparty | None:
        if reference is None:
            return None
        if reference.id:
            counterparty = self.session.scalar(
                select(Counterparty).where(
                    Counterparty.id == reference.id, Counterparty.org_id == org_id
                )
            )
            if counterparty is None:
                raise ValueError("counterparty not found in this organization")
            return counterparty
        counterparty = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == reference.kind,
                Counterparty.name == reference.name,
            )
        )
        if counterparty is None:
            counterparty = Counterparty(
                org_id=org_id,
                kind=reference.kind or "other",
                name=reference.name or "",
                external_ref=reference.external_ref,
            )
            self.session.add(counterparty)
            self.session.flush()
        return counterparty

    def _resolve_counterparty(self, request: RecordEventRequest) -> Counterparty | None:
        return self._resolve_counterparty_reference(request.org_id, request.counterparty)

    def _validate_business_links(self, request: RecordEventRequest) -> BusinessEvent | None:
        settlement_date = self._bank_settlement_date(request)
        for _side, account_code in self._bank_account_selections(request):
            if account_code is not None:
                self._validate_bank_account(request.org_id, account_code, settlement_date)

        if request.event_type == EventType.INTERNAL_TRANSFER:
            return None

        if request.event_type == EventType.TAX_PAYMENT:
            role = {
                "vat": "vat_payable",
                "surtax": "surtax_payable",
                "enterprise_income_tax": "enterprise_income_tax_payable",
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
            principal_fen = self._amount(request) - int(
                request.details.owner_repayment_fee_fen or 0
            )
            if principal_fen > payable:
                raise ValueError(
                    f"owner repayment exceeds payable balance: available={payable}, "
                    f"requested={principal_fen}"
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
            return original

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
            select(BusinessEvent)
            .where(
                BusinessEvent.id == original_id,
                BusinessEvent.org_id == request.org_id,
            )
            .with_for_update()
        )
        if original is None:
            raise ValueError("linked original event was not found")
        return original

    def _linked_usage_fen(self, org_id: uuid.UUID, original_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.coalesce(func.sum(BusinessEventDependency.amount_fen), 0))
                .join(
                    BusinessEvent,
                    (BusinessEvent.org_id == BusinessEventDependency.org_id)
                    & (BusinessEvent.id == BusinessEventDependency.child_event_id),
                )
                .where(
                    BusinessEventDependency.org_id == org_id,
                    BusinessEventDependency.parent_event_id == original_id,
                    BusinessEvent.status == "posted",
                )
            )
            or 0
        )

    @staticmethod
    def _business_dependency_kind(request: RecordEventRequest) -> str:
        if request.event_type == EventType.SERVICE_FULFILLMENT:
            return "advance_fulfillment"
        if request.event_type == EventType.CUSTOMER_REFUND:
            return (
                "advance_refund"
                if request.details.get("refund_kind") == "advance"
                else "sale_return"
            )
        raise ValueError("BUSINESS_EVENT_DEPENDENCY_INVALID")

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
        value = amounts.get("gross_amount_fen")
        if value is None:
            value = amounts.get("amount_fen")
        if value is None:
            raise ValueError(f"event {event.id} has no amount")
        return int(value)

    def _missing_information(self, request: RecordEventRequest) -> list[str]:
        missing: list[str] = []
        event_type = request.event_type
        amount = request.amounts.amount_fen
        if amount is None:
            amount = request.amounts.gross_amount_fen
        if amount is None:
            missing.append("amounts.amount_fen or amounts.gross_amount_fen")

        if request.event_type is EventType.INTERNAL_TRANSFER:
            if request.source_bank_account_code is None:
                missing.append("source_bank_account_code")
            if request.destination_bank_account_code is None:
                missing.append("destination_bank_account_code")
            if (
                request.source_bank_account_code is not None
                and request.source_bank_account_code == request.destination_bank_account_code
            ):
                missing.append("different source and destination bank accounts")
        elif self._uses_bank_settlement(request) and request.bank_account_code is None:
            missing.append("bank_account_code")
        if request.event_type in {
            EventType.CASH_BANK_TRANSFER,
            EventType.PAYMENT_PLATFORM_TRANSFER,
        } and request.direction is None:
            missing.append("direction")

        counterparty_events = {
            EventType.SERVICE_CREDIT_SALE,
            EventType.SERVICE_FULFILLMENT,
            EventType.CUSTOMER_RECEIPT,
            EventType.CUSTOMER_ADVANCE,
            EventType.CUSTOMER_REFUND,
            EventType.EXPENSE_PAYABLE,
            EventType.SUPPLIER_PAYMENT,
            EventType.EMPLOYEE_REIMBURSEMENT,
            EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT,
            EventType.OWNER_LOAN_RECEIVED,
            EventType.OWNER_CONTRIBUTION_RECEIVED,
            EventType.OWNER_REPAYMENT,
            EventType.OTHER_INCOME_RECEIVED,
            EventType.REFUNDABLE_DEPOSIT_PAID,
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
        }
        if event_type in counterparty_events and request.counterparty is None:
            missing.append("counterparty")
        if (
            event_type == EventType.EMPLOYEE_REIMBURSEMENT
            and request.details.reimbursement_kind == "refundable_deposit"
            and request.deposit_holder is None
        ):
            missing.append("deposit_holder")

        if event_type == EventType.OTHER_INCOME_RECEIVED:
            if request.details.other_income_kind != "retained_verification_payment":
                missing.append("details.other_income_kind='retained_verification_payment'")
            if not request.bank_transaction_references:
                missing.append("bank_transaction_references")
            if not request.evidence_references:
                missing.append("evidence_references")
            if not request.description.strip():
                missing.append("description")

        if event_type == EventType.BANK_INTEREST_RECEIVED:
            if not request.bank_transaction_references:
                missing.append("bank_transaction_references")
            if not request.evidence_references:
                missing.append("evidence_references")
            if not request.description.strip():
                missing.append("description")

        if event_type == EventType.EXPENSE_RECOVERY_RECEIVED:
            if request.details.expense_recovery_kind != "owner_managed_payment_account_return":
                missing.append(
                    "details.expense_recovery_kind='owner_managed_payment_account_return'"
                )
            if not request.bank_transaction_references:
                missing.append("bank_transaction_references")
            if not request.evidence_references:
                missing.append("evidence_references")
            if not request.description.strip():
                missing.append("description")

        if event_type == EventType.PAYMENT_PLATFORM_TRANSFER:
            if not request.bank_transaction_references:
                missing.append("bank_transaction_references")
            if not request.evidence_references:
                missing.append("evidence_references")
            if not request.description.strip():
                missing.append("description")

        if event_type in {
            EventType.REFUNDABLE_DEPOSIT_PAID,
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
        }:
            if not request.bank_transaction_references:
                missing.append("bank_transaction_references")
            if not request.evidence_references:
                missing.append("evidence_references")
            if not request.description.strip():
                missing.append("description")

        sales_events = {
            EventType.SERVICE_CASH_SALE,
            EventType.SERVICE_CREDIT_SALE,
            EventType.SERVICE_FULFILLMENT,
            EventType.CUSTOMER_ADVANCE,
        }
        if event_type in sales_events and request.tax_facts is None:
            missing.append("tax_facts")
        tax_facts_required = event_type in sales_events or (
            event_type == EventType.CUSTOMER_REFUND
            and request.details.get("refund_kind") == "sale_return"
        )
        if tax_facts_required and request.tax_facts is not None:
            for field_name in (
                "taxable",
                "rate_percent",
                "invoice_type",
                "waive_exemption",
                "tax_due_on_event",
            ):
                if getattr(request.tax_facts, field_name) is None:
                    missing.append(f"tax_facts.{field_name}")
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

        if event_type in {
            EventType.SUPPLIER_PAYMENT,
            EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT,
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
        }:
            allocated = sum(item.amount_fen for item in request.allocations)
            if not request.allocations:
                missing.append("allocations")
            elif amount and allocated != amount:
                missing.append("allocations whose total equals the payment")

        if self._payroll_payment_categories(event_type) is not None:
            allocated = sum(item.amount_fen for item in request.allocations)
            statutory_late_fee_fen = (
                int(request.details.social_insurance_late_fee_fen or 0)
                if event_type is EventType.SOCIAL_INSURANCE_PAYMENT
                else 0
            )
            if not request.allocations:
                missing.append("allocations")
            elif (
                event_type != EventType.SALARY_PAYMENT
                and amount
                and allocated + statutory_late_fee_fen != amount
            ):
                missing.append("allocations whose total equals the payment")
            if statutory_late_fee_fen:
                if not request.bank_transaction_references:
                    missing.append("bank_transaction_references")
                if not request.evidence_references:
                    missing.append("evidence_references")
                if not request.description:
                    missing.append("description")
            if event_type == EventType.SALARY_PAYMENT:
                withholding_ids = {
                    item.open_item_id for item in request.salary_withholding_allocations
                }
                allocation_ids = {item.open_item_id for item in request.allocations}
                if not request.salary_withholding_allocations:
                    missing.append("salary_withholding_allocations")
                elif withholding_ids != allocation_ids:
                    missing.append(
                        "salary_withholding_allocations for each allocated salary open item"
                    )
                elif amount and allocated < amount:
                    missing.append("salary allocations exceed cash payment after withholdings")

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
        if event_type == EventType.TAX_PAYMENT and request.details.get("tax_type") not in {
            "vat",
            "surtax",
            "enterprise_income_tax",
        }:
            missing.append("details.tax_type ('vat', 'surtax', or 'enterprise_income_tax')")
        expense_events = {
            EventType.EXPENSE_CASH,
            EventType.EXPENSE_RECOVERY_RECEIVED,
            EventType.EXPENSE_PAYABLE,
        }
        if (
            event_type == EventType.EMPLOYEE_REIMBURSEMENT
            and request.details.reimbursement_kind in {None, "expense"}
        ):
            expense_events.add(EventType.EMPLOYEE_REIMBURSEMENT)
        if event_type in expense_events:
            if request.amounts.expense_account_role is None:
                missing.append("amounts.expense_account_role")
            elif request.amounts.expense_account_role not in {
                "service_cost",
                "sales_expense",
                "general_expense",
                "finance_expense",
                "labor_service_cost",
            }:
                missing.append("a supported amounts.expense_account_role")

        required_dates: dict[EventType, tuple[str, ...]] = {
            EventType.SERVICE_CASH_SALE: ("fulfillment_date", "payment_date"),
            EventType.SERVICE_CREDIT_SALE: ("fulfillment_date",),
            EventType.SERVICE_FULFILLMENT: ("fulfillment_date",),
            EventType.CUSTOMER_RECEIPT: ("payment_date",),
            EventType.CUSTOMER_ADVANCE: ("payment_date",),
            EventType.CUSTOMER_REFUND: ("payment_date",),
            EventType.EXPENSE_CASH: ("payment_date",),
            EventType.EXPENSE_RECOVERY_RECEIVED: ("payment_date",),
            EventType.SUPPLIER_PAYMENT: ("payment_date",),
            EventType.EMPLOYEE_REIMBURSEMENT_PAYMENT: ("payment_date",),
            EventType.OWNER_LOAN_RECEIVED: ("payment_date",),
            EventType.OWNER_CONTRIBUTION_RECEIVED: ("payment_date",),
            EventType.OWNER_REPAYMENT: ("payment_date",),
            EventType.OTHER_INCOME_RECEIVED: ("payment_date",),
            EventType.BANK_INTEREST_RECEIVED: ("payment_date",),
            EventType.REFUNDABLE_DEPOSIT_PAID: ("payment_date",),
            EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED: ("payment_date",),
            EventType.BANK_FEE: ("payment_date",),
            EventType.TAX_PAYMENT: ("payment_date",),
            EventType.SALARY_PAYMENT: ("payment_date",),
            EventType.SOCIAL_INSURANCE_PAYMENT: ("payment_date",),
            EventType.HOUSING_FUND_PAYMENT: ("payment_date",),
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT: ("payment_date",),
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
            request_payload_hash=FinanceService._canonical_payload_hash(
                request.model_dump(mode="json")
            ),
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

    def register_employee(self, request: RegisterEmployeeRequest) -> dict[str, Any]:
        if self.session.get(Organization, request.org_id) is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        existing = self.session.scalar(
            select(Employee).where(
                Employee.org_id == request.org_id,
                Employee.employee_code == request.employee_code,
            )
        )
        if existing is not None:
            same_identity_and_employment = (
                existing.name == request.name
                and existing.employment_start_date == request.employment_start_date
                and existing.employment_end_date == request.employment_end_date
                and existing.status == request.status
                and existing.prior_labor_person_id == request.prior_labor_person_id
            )
            if not same_identity_and_employment:
                return {"status": "rejected", "errors": ["EMPLOYEE_CODE_ALREADY_EXISTS"]}
            if (
                existing.tax_withholding_start_date is None
                and request.tax_withholding_start_date is not None
            ):
                existing.tax_withholding_start_date = request.tax_withholding_start_date
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        action="payroll_employee_tax_withholding_start_registered",
                        details={
                            "employee_id": str(existing.id),
                            "employee_code": existing.employee_code,
                            "tax_withholding_start_date": (
                                request.tax_withholding_start_date.isoformat()
                            ),
                        },
                    )
                )
                self.session.flush()
                return {
                    "status": "registered",
                    "employee_id": str(existing.id),
                    "tax_withholding_start_date_registered": True,
                }
            if existing.tax_withholding_start_date != request.tax_withholding_start_date:
                return {"status": "rejected", "errors": ["EMPLOYEE_CODE_ALREADY_EXISTS"]}
            return {
                "status": "registered",
                "employee_id": str(existing.id),
                "idempotent_replay": True,
            }

        prior_labor_person = None
        if request.prior_labor_person_id is not None:
            prior_labor_person = self.session.scalar(
                select(LaborServicePerson).where(
                    LaborServicePerson.org_id == request.org_id,
                    LaborServicePerson.id == request.prior_labor_person_id,
                )
            )
            if prior_labor_person is None:
                return {
                    "status": "rejected",
                    "errors": ["PRIOR_LABOR_PERSON_NOT_FOUND_OR_ORGANIZATION_MISMATCH"],
                }
            if (
                prior_labor_person.status != "ended"
                or prior_labor_person.relationship_end_date is None
                or prior_labor_person.relationship_end_date >= request.employment_start_date
            ):
                return {
                    "status": "rejected",
                    "errors": ["LABOR_RELATIONSHIP_MUST_END_BEFORE_EMPLOYMENT"],
                }
            if self.session.scalar(
                select(Employee.id).where(Employee.prior_labor_person_id == prior_labor_person.id)
            ):
                return {
                    "status": "rejected",
                    "errors": ["LABOR_PERSON_ALREADY_LINKED_TO_EMPLOYEE"],
                }
            if prior_labor_person.name != request.name:
                return {
                    "status": "rejected",
                    "errors": ["LABOR_TO_EMPLOYEE_IDENTITY_NAME_MISMATCH"],
                }

        # Counterparty names are deliberately code-qualified: two employees may share a name,
        # while the underlying counterparty identity remains deterministic and non-sensitive.
        counterparty_name = f"员工 {request.employee_code}"
        counterparty = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == request.org_id,
                Counterparty.kind == "employee",
                Counterparty.name == counterparty_name,
            )
        )
        if counterparty is None:
            counterparty = Counterparty(
                org_id=request.org_id,
                kind="employee",
                name=counterparty_name,
                external_ref=request.employee_code,
            )
            self.session.add(counterparty)
            self.session.flush()
        employee = Employee(
            org_id=request.org_id,
            counterparty_id=counterparty.id,
            prior_labor_person_id=(prior_labor_person.id if prior_labor_person else None),
            employee_code=request.employee_code,
            name=request.name,
            employment_start_date=request.employment_start_date,
            tax_withholding_start_date=request.tax_withholding_start_date,
            employment_end_date=request.employment_end_date,
            status=request.status,
        )
        self.session.add(employee)
        self.session.flush()
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                action="payroll_employee_registered",
                details={
                    "employee_id": str(employee.id),
                    "employee_code": employee.employee_code,
                    "employment_start_date": employee.employment_start_date.isoformat(),
                    "tax_withholding_start_date": (
                        employee.tax_withholding_start_date.isoformat()
                        if employee.tax_withholding_start_date
                        else None
                    ),
                },
            )
        )
        return {"status": "registered", "employee_id": str(employee.id)}

    def register_employee_payroll_profile_version(
        self, request: RegisterEmployeePayrollProfileVersionRequest
    ) -> dict[str, Any]:
        employee = self._employee_for_org(request.org_id, request.employee_id)
        if employee is None:
            return {"status": "rejected", "errors": ["EMPLOYEE_NOT_FOUND"]}
        expected = request.model_dump(
            exclude={"org_id", "employee_id", "supersedes_profile_version_id"}
        )
        predecessor = None
        if request.supersedes_profile_version_id is not None:
            predecessor = self.session.scalar(
                select(EmployeePayrollProfileVersion)
                .where(EmployeePayrollProfileVersion.id == request.supersedes_profile_version_id)
                .with_for_update()
            )
            if (
                predecessor is None
                or predecessor.org_id != request.org_id
                or predecessor.employee_id != employee.id
            ):
                return {"status": "rejected", "errors": ["INVALID_PROFILE_VERSION_SUPERSEDES"]}
            existing_successor = self.session.scalar(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.supersedes_id == predecessor.id
                )
            )
            if existing_successor is not None:
                actual = {
                    "effective_from": existing_successor.effective_from,
                    "effective_to": existing_successor.effective_to,
                    "expense_role": existing_successor.expense_role,
                    "social_insurance_base_fen": existing_successor.social_insurance_base_fen,
                    "housing_fund_base_fen": existing_successor.housing_fund_base_fen,
                    "social_insurance_participating": (
                        existing_successor.social_insurance_participating
                    ),
                    "housing_fund_participating": (
                        existing_successor.housing_fund_participating
                    ),
                    "resident_employee": existing_successor.resident_employee,
                }
                if actual == expected:
                    return {
                        "status": "registered",
                        "profile_version_id": str(existing_successor.id),
                        "idempotent_replay": True,
                    }
                return {"status": "rejected", "errors": ["PROFILE_VERSION_SUCCESSOR_EXISTS"]}
        else:
            existing = self.session.scalar(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.org_id == request.org_id,
                    EmployeePayrollProfileVersion.employee_id == request.employee_id,
                    EmployeePayrollProfileVersion.effective_from == request.effective_from,
                )
            )
            if existing is not None:
                actual = {
                    "effective_from": existing.effective_from,
                    "effective_to": existing.effective_to,
                    "expense_role": existing.expense_role,
                    "social_insurance_base_fen": existing.social_insurance_base_fen,
                    "housing_fund_base_fen": existing.housing_fund_base_fen,
                    "social_insurance_participating": existing.social_insurance_participating,
                    "housing_fund_participating": existing.housing_fund_participating,
                    "resident_employee": existing.resident_employee,
                }
                if actual == expected:
                    return {
                        "status": "registered",
                        "profile_version_id": str(existing.id),
                        "idempotent_replay": True,
                    }
                return {"status": "rejected", "errors": ["PROFILE_VERSION_ALREADY_EXISTS"]}
            overlapping = self.session.scalars(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.org_id == request.org_id,
                    EmployeePayrollProfileVersion.employee_id == request.employee_id,
                )
            ).all()
            if any(
                self._effective_date_ranges_overlap(
                    request.effective_from,
                    request.effective_to,
                    candidate.effective_from,
                    candidate.effective_to,
                )
                for candidate in overlapping
            ):
                return {"status": "rejected", "errors": ["OVERLAPPING_EMPLOYEE_PROFILE_VERSION"]}
        if predecessor is not None:
            candidates = self.session.scalars(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.org_id == request.org_id,
                    EmployeePayrollProfileVersion.employee_id == request.employee_id,
                )
            ).all()
            if error := self._successor_lineage_error(
                predecessor=predecessor,
                version_model=EmployeePayrollProfileVersion,
                candidates=candidates,
                effective_from=request.effective_from,
                effective_to=request.effective_to,
                error_prefix="PAYROLL_PROFILE_VERSION",
            ):
                return {"status": "rejected", "errors": [error]}
            if blocking_batch_ids := self._profile_correction_blocking_batches(
                request.org_id,
                employee.id,
                predecessor.id,
                request.effective_from,
                request.effective_to,
            ):
                return self._blocked_payroll_version_correction(blocking_batch_ids)
        version = EmployeePayrollProfileVersion(
            org_id=request.org_id,
            employee_id=request.employee_id,
            supersedes_id=predecessor.id if predecessor is not None else None,
            effective_from=request.effective_from,
            effective_to=request.effective_to,
            expense_role=request.expense_role,
            social_insurance_base_fen=request.social_insurance_base_fen,
            housing_fund_base_fen=request.housing_fund_base_fen,
            social_insurance_participating=request.social_insurance_participating,
            housing_fund_participating=request.housing_fund_participating,
            resident_employee=request.resident_employee,
        )
        try:
            with self.session.begin_nested():
                self.session.add(version)
                self.session.flush()
                self._assert_round6_final_dependency_constraints_now()
        except DBAPIError as exc:
            if not self._is_round6_final_dependency_error(exc):
                raise
            blocking_batch_ids = self._profile_correction_blocking_batches(
                request.org_id,
                employee.id,
                predecessor.id if predecessor is not None else version.id,
                request.effective_from,
                request.effective_to,
            )
            return self._blocked_payroll_version_correction(blocking_batch_ids)
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                action="payroll_profile_version_registered",
                details={
                    "employee_id": str(employee.id),
                    "profile_version_id": str(version.id),
                    "supersedes_profile_version_id": (
                        str(request.supersedes_profile_version_id)
                        if request.supersedes_profile_version_id
                        else None
                    ),
                },
            )
        )
        return {"status": "registered", "profile_version_id": str(version.id)}

    def register_payroll_policy_version(
        self, request: RegisterPayrollPolicyVersionRequest
    ) -> dict[str, Any]:
        if self.session.get(Organization, request.org_id) is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        parsed_url = urlparse(request.source_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return {"status": "rejected", "errors": ["INVALID_POLICY_SOURCE_URL"]}
        try:
            # Validate the full effective-dated calculator contract before any
            # policy is persisted.  In particular this rejects implicit source
            # URL/version fallbacks and binary floating-point rates.
            candidate = PayrollPolicyVersion(**self._payroll_policy_values(request))
            self._calculator_policies(candidate)
            self._payment_targets(candidate.parameters)
        except CalculationValidationError as exc:
            return {"status": "rejected", "errors": [f"{exc.code}:{exc}"]}
        except ValidationError:
            return {"status": "rejected", "errors": ["INVALID_POLICY_PARAMETERS"]}
        existing = self.session.scalar(
            select(PayrollPolicyVersion).where(
                PayrollPolicyVersion.org_id == request.org_id,
                PayrollPolicyVersion.region == request.region,
                PayrollPolicyVersion.version == request.version,
            )
        )
        if existing is not None:
            expected = self._payroll_policy_values(request)
            for field_name in ("org_id", "region", "version", "supersedes_id"):
                expected.pop(field_name)
            actual = {
                "effective_from": existing.effective_from,
                "effective_to": existing.effective_to,
                "source_url": existing.source_url,
                "parameters": existing.parameters,
            }
            if actual != expected:
                return {"status": "rejected", "errors": ["PAYROLL_POLICY_VERSION_ALREADY_EXISTS"]}
            return {
                "status": "registered",
                "policy_version_id": str(existing.id),
                "idempotent_replay": True,
            }
        predecessor = None
        if request.supersedes_policy_version_id is not None:
            predecessor = self.session.scalar(
                select(PayrollPolicyVersion)
                .where(PayrollPolicyVersion.id == request.supersedes_policy_version_id)
                .with_for_update()
            )
            if (
                predecessor is None
                or predecessor.org_id != request.org_id
                or predecessor.region != request.region
            ):
                return {"status": "rejected", "errors": ["INVALID_PAYROLL_POLICY_SUPERSEDES"]}
            existing_successor = self.session.scalar(
                select(PayrollPolicyVersion).where(
                    PayrollPolicyVersion.supersedes_id == predecessor.id
                )
            )
            if existing_successor is not None:
                expected = self._payroll_policy_values(request)
                for field_name in ("org_id", "region", "version", "supersedes_id"):
                    expected.pop(field_name)
                actual = {
                    "effective_from": existing_successor.effective_from,
                    "effective_to": existing_successor.effective_to,
                    "source_url": existing_successor.source_url,
                    "parameters": existing_successor.parameters,
                }
                if actual == expected:
                    return {
                        "status": "registered",
                        "policy_version_id": str(existing_successor.id),
                        "idempotent_replay": True,
                    }
                return {"status": "rejected", "errors": ["PAYROLL_POLICY_SUCCESSOR_EXISTS"]}
        else:
            overlapping = self.session.scalars(
                select(PayrollPolicyVersion).where(
                    PayrollPolicyVersion.org_id == request.org_id,
                    PayrollPolicyVersion.region == request.region,
                )
            ).all()
            if any(
                self._effective_date_ranges_overlap(
                    request.effective_from,
                    request.effective_to,
                    candidate.effective_from,
                    candidate.effective_to,
                )
                for candidate in overlapping
            ):
                return {"status": "rejected", "errors": ["OVERLAPPING_PAYROLL_POLICY_VERSION"]}
        if predecessor is not None:
            candidates = self.session.scalars(
                select(PayrollPolicyVersion).where(
                    PayrollPolicyVersion.org_id == request.org_id,
                    PayrollPolicyVersion.region == request.region,
                )
            ).all()
            if error := self._successor_lineage_error(
                predecessor=predecessor,
                version_model=PayrollPolicyVersion,
                candidates=candidates,
                effective_from=request.effective_from,
                effective_to=request.effective_to,
                error_prefix="PAYROLL_POLICY_VERSION",
            ):
                return {"status": "rejected", "errors": [error]}
            if blocking_batch_ids := self._policy_correction_blocking_batches(
                request.org_id,
                predecessor.id,
                request.effective_from,
                request.effective_to,
            ):
                return self._blocked_payroll_version_correction(blocking_batch_ids)
        policy = PayrollPolicyVersion(**self._payroll_policy_values(request))
        try:
            with self.session.begin_nested():
                self.session.add(policy)
                self.session.flush()
                self._assert_round6_final_dependency_constraints_now()
        except DBAPIError as exc:
            if not self._is_round6_final_dependency_error(exc):
                raise
            blocking_batch_ids = self._policy_correction_blocking_batches(
                request.org_id,
                predecessor.id if predecessor is not None else policy.id,
                request.effective_from,
                request.effective_to,
            )
            return self._blocked_payroll_version_correction(blocking_batch_ids)
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                action="payroll_policy_version_registered",
                details={
                    "policy_version_id": str(policy.id),
                    "version": policy.version,
                    "supersedes_policy_version_id": (
                        str(request.supersedes_policy_version_id)
                        if request.supersedes_policy_version_id
                        else None
                    ),
                },
            )
        )
        return {"status": "registered", "policy_version_id": str(policy.id)}

    def register_payroll_opening_state(
        self, request: RegisterPayrollOpeningStateRequest
    ) -> dict[str, Any]:
        if self._employee_for_org(request.org_id, request.employee_id) is None:
            return {"status": "rejected", "errors": ["EMPLOYEE_NOT_FOUND"]}
        values = request.model_dump(
            exclude={"org_id", "employee_id", "supersedes_opening_state_id"}
        )
        predecessor = None
        if request.supersedes_opening_state_id is not None:
            predecessor = self.session.scalar(
                select(PayrollOpeningState)
                .where(PayrollOpeningState.id == request.supersedes_opening_state_id)
                .with_for_update()
            )
            if (
                predecessor is None
                or predecessor.org_id != request.org_id
                or predecessor.employee_id != request.employee_id
                or predecessor.tax_year != request.tax_year
                or predecessor.through_month != request.through_month
            ):
                return {"status": "rejected", "errors": ["INVALID_OPENING_STATE_SUPERSEDES"]}
            existing_successor = self.session.scalar(
                select(PayrollOpeningState).where(
                    PayrollOpeningState.supersedes_id == predecessor.id
                )
            )
            if existing_successor is not None:
                actual = {key: getattr(existing_successor, key) for key in values}
                if actual == values:
                    return {
                        "status": "registered",
                        "opening_state_id": str(existing_successor.id),
                        "idempotent_replay": True,
                    }
                return {"status": "rejected", "errors": ["OPENING_STATE_SUCCESSOR_EXISTS"]}
        else:
            existing = self.session.scalar(
                select(PayrollOpeningState).where(
                    PayrollOpeningState.org_id == request.org_id,
                    PayrollOpeningState.employee_id == request.employee_id,
                    PayrollOpeningState.tax_year == request.tax_year,
                    PayrollOpeningState.through_month == request.through_month,
                )
            )
            if existing is not None:
                actual = {key: getattr(existing, key) for key in values}
                if actual != values:
                    return {
                        "status": "rejected",
                        "errors": ["OPENING_STATE_CORRECTION_REQUIRES_NEW_VERSION"],
                    }
                return {
                    "status": "registered",
                    "opening_state_id": str(existing.id),
                    "idempotent_replay": True,
                }
        if predecessor is not None:
            candidates = self.session.scalars(
                select(PayrollOpeningState).where(
                    PayrollOpeningState.org_id == request.org_id,
                    PayrollOpeningState.employee_id == request.employee_id,
                    PayrollOpeningState.tax_year == request.tax_year,
                    PayrollOpeningState.through_month == request.through_month,
                )
            ).all()
            if error := self._successor_lineage_error(
                predecessor=predecessor,
                version_model=PayrollOpeningState,
                candidates=candidates,
                effective_from=None,
                effective_to=None,
                error_prefix="PAYROLL_OPENING_STATE",
            ):
                return {"status": "rejected", "errors": [error]}
        if blocking_batch_ids := self._opening_correction_blocking_batches(
            request.org_id, request.employee_id, request.tax_year, request.through_month
        ):
            return self._blocked_payroll_version_correction(blocking_batch_ids)
        opening = PayrollOpeningState(
            org_id=request.org_id,
            employee_id=request.employee_id,
            supersedes_id=predecessor.id if predecessor is not None else None,
            **values,
        )
        try:
            with self.session.begin_nested():
                self.session.add(opening)
                self.session.flush()
                self._assert_round6_final_dependency_constraints_now()
        except DBAPIError as exc:
            if not self._is_round6_final_dependency_error(exc):
                raise
            blocking_batch_ids = self._opening_correction_blocking_batches(
                request.org_id,
                request.employee_id,
                request.tax_year,
                request.through_month,
            )
            return self._blocked_payroll_version_correction(blocking_batch_ids)
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                action="payroll_opening_state_registered",
                details={
                    "employee_id": str(request.employee_id),
                    "opening_state_id": str(opening.id),
                    "supersedes_opening_state_id": (
                        str(request.supersedes_opening_state_id)
                        if request.supersedes_opening_state_id
                        else None
                    ),
                },
            )
        )
        return {"status": "registered", "opening_state_id": str(opening.id)}

    def _active_first_wage_tax_treatment(
        self, org_id: uuid.UUID, employee_id: uuid.UUID, tax_year: int
    ) -> PayrollFirstWageTaxTreatment | None:
        successor = aliased(PayrollFirstWageTaxTreatment)
        matches = list(
            self.session.scalars(
                select(PayrollFirstWageTaxTreatment)
                .where(
                    PayrollFirstWageTaxTreatment.org_id == org_id,
                    PayrollFirstWageTaxTreatment.employee_id == employee_id,
                    PayrollFirstWageTaxTreatment.tax_year == tax_year,
                    ~exists(
                        select(successor.id).where(
                            successor.supersedes_id == PayrollFirstWageTaxTreatment.id
                        )
                    ),
                )
                .order_by(PayrollFirstWageTaxTreatment.created_at.desc())
            )
        )
        if len(matches) > 1:
            raise CalculationValidationError(
                "AMBIGUOUS_FIRST_WAGE_TAX_TREATMENT",
                "more than one first-wage tax treatment is active for the employee-year",
            )
        return matches[0] if matches else None

    def register_payroll_first_wage_tax_treatment(
        self, request: RegisterPayrollFirstWageTaxTreatmentRequest
    ) -> dict[str, Any]:
        """Register the evidenced employee-year treatment without changing employment dates."""

        payload_hash = self._request_payload_hash(request)
        existing = self.session.scalar(
            select(PayrollFirstWageTaxTreatment).where(
                PayrollFirstWageTaxTreatment.org_id == request.org_id,
                PayrollFirstWageTaxTreatment.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                return {
                    "status": "rejected",
                    "errors": ["FIRST_WAGE_TAX_TREATMENT_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                }
            return {
                "status": "registered",
                "treatment_id": str(existing.id),
                "idempotent_replay": True,
            }
        employee = self._employee_for_org(request.org_id, request.employee_id)
        if employee is None:
            return {"status": "rejected", "errors": ["EMPLOYEE_NOT_FOUND"]}
        tax_start = employee.tax_withholding_start_date
        if (
            tax_start is None
            or tax_start.year != request.tax_year
            or tax_start.month != request.first_wage_month
        ):
            return {
                "status": "rejected",
                "errors": ["FIRST_WAGE_MONTH_MUST_MATCH_TAX_WITHHOLDING_START"],
            }
        try:
            evidence_ids = self._validate_payroll_batch_evidence(
                request.org_id, request.evidence_references
            )
        except CalculationValidationError as exc:
            return {"status": "rejected", "errors": [exc.code]}
        active = self._active_first_wage_tax_treatment(
            request.org_id, request.employee_id, request.tax_year
        )
        predecessor = None
        if request.supersedes_treatment_id is not None:
            predecessor = self.session.scalar(
                select(PayrollFirstWageTaxTreatment)
                .where(
                    PayrollFirstWageTaxTreatment.org_id == request.org_id,
                    PayrollFirstWageTaxTreatment.id == request.supersedes_treatment_id,
                )
                .with_for_update()
            )
            if (
                predecessor is None
                or predecessor.employee_id != request.employee_id
                or predecessor.tax_year != request.tax_year
                or active is None
                or predecessor.id != active.id
            ):
                return {"status": "rejected", "errors": ["INVALID_FIRST_WAGE_SUPERSEDES"]}
        elif active is not None:
            return {
                "status": "rejected",
                "errors": ["FIRST_WAGE_TREATMENT_CORRECTION_REQUIRES_SUPERSEDES"],
            }
        calculated_batches_to_supersede: list[PayrollBatch] = []
        if predecessor is not None:
            uses = self.session.execute(
                select(PayrollFirstWageTaxTreatmentUse, PayrollBatch)
                .join(
                    PayrollBatch,
                    (PayrollBatch.org_id == PayrollFirstWageTaxTreatmentUse.org_id)
                    & (PayrollBatch.id == PayrollFirstWageTaxTreatmentUse.payroll_batch_id),
                )
                .where(
                    PayrollFirstWageTaxTreatmentUse.org_id == request.org_id,
                    PayrollFirstWageTaxTreatmentUse.treatment_id == predecessor.id,
                    PayrollBatch.status.in_(("calculated", "posted")),
                )
                .with_for_update()
            ).all()
            posted_ids = sorted({str(batch.id) for _, batch in uses if batch.status == "posted"})
            if posted_ids:
                return {
                    "status": "rejected",
                    "errors": ["FIRST_WAGE_POSTED_PAYROLL_MUST_BE_REVERSED_FIRST"],
                    "blocking_payroll_batch_ids": posted_ids,
                }
            calculated_batches_to_supersede = [batch for _, batch in uses]
        treatment = PayrollFirstWageTaxTreatment(
            org_id=request.org_id,
            employee_id=request.employee_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            tax_year=request.tax_year,
            first_wage_month=request.first_wage_month,
            treatment_state=request.treatment_state.value,
            declaration_date=request.declaration_date,
            confirmation_description=request.confirmation_description,
            legal_basis_url=self.FIRST_WAGE_TAX_TREATMENT_SOURCE_URL,
            supersedes_id=predecessor.id if predecessor is not None else None,
        )
        try:
            with self.session.begin_nested():
                for batch in calculated_batches_to_supersede:
                    batch.status = "superseded"
                self.session.add(treatment)
                self.session.flush()
                for evidence_id in evidence_ids:
                    self.session.add(
                        PayrollFirstWageTaxTreatmentEvidence(
                            org_id=request.org_id,
                            treatment_id=treatment.id,
                            evidence_id=evidence_id,
                        )
                    )
                self.session.flush()
        except IntegrityError:
            concurrent = self.session.scalar(
                select(PayrollFirstWageTaxTreatment).where(
                    PayrollFirstWageTaxTreatment.org_id == request.org_id,
                    PayrollFirstWageTaxTreatment.idempotency_key == request.idempotency_key,
                )
            )
            if concurrent is not None and concurrent.request_payload_hash == payload_hash:
                return {
                    "status": "registered",
                    "treatment_id": str(concurrent.id),
                    "idempotent_replay": True,
                }
            return {
                "status": "rejected",
                "errors": ["FIRST_WAGE_TAX_TREATMENT_CONCURRENT_WRITE_CONFLICT"],
            }
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                action="payroll_first_wage_tax_treatment_registered",
                details={
                    "treatment_id": str(treatment.id),
                    "employee_id": str(request.employee_id),
                    "tax_year": request.tax_year,
                    "first_wage_month": request.first_wage_month,
                    "treatment_state": request.treatment_state.value,
                    "supersedes_treatment_id": (
                        str(request.supersedes_treatment_id)
                        if request.supersedes_treatment_id
                        else None
                    ),
                    "legal_basis_url": self.FIRST_WAGE_TAX_TREATMENT_SOURCE_URL,
                },
            )
        )
        return {"status": "registered", "treatment_id": str(treatment.id)}

    @staticmethod
    def _contribution_actual_result(actual_set: PayrollContributionActualSet) -> dict[str, Any]:
        return {
            "status": "registered",
            "actual_set_id": str(actual_set.id),
            "employee_id": str(actual_set.employee_id),
            "contribution_period": actual_set.contribution_period,
        }

    def _active_contribution_actual_items(
        self, org_id: uuid.UUID, employee_id: uuid.UUID, contribution_period: str
    ) -> list[PayrollContributionActualItem]:
        successor = aliased(PayrollContributionActualItem)
        return list(
            self.session.scalars(
                select(PayrollContributionActualItem)
                .where(
                    PayrollContributionActualItem.org_id == org_id,
                    PayrollContributionActualItem.employee_id == employee_id,
                    PayrollContributionActualItem.contribution_period == contribution_period,
                    ~exists(
                        select(successor.id).where(
                            successor.supersedes_id == PayrollContributionActualItem.id
                        )
                    ),
                )
                .order_by(
                    PayrollContributionActualItem.contribution_group,
                    PayrollContributionActualItem.insurance_kind,
                    PayrollContributionActualItem.id,
                )
            )
        )

    def register_payroll_contribution_actual(
        self, request: RegisterPayrollContributionActualRequest
    ) -> dict[str, Any]:
        """Persist sparse, evidenced actual amounts without mutating company policy."""

        payload_hash = self._request_payload_hash(request)
        existing_set = self.session.scalar(
            select(PayrollContributionActualSet).where(
                PayrollContributionActualSet.org_id == request.org_id,
                PayrollContributionActualSet.idempotency_key == request.idempotency_key,
            )
        )
        if existing_set is not None:
            if existing_set.request_payload_hash != payload_hash:
                return {
                    "status": "rejected",
                    "errors": ["PAYROLL_CONTRIBUTION_ACTUAL_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                }
            return {
                **self._contribution_actual_result(existing_set),
                "actual_item_ids": [
                    str(value)
                    for value in self.session.scalars(
                        select(PayrollContributionActualItem.id)
                        .where(
                            PayrollContributionActualItem.org_id == request.org_id,
                            PayrollContributionActualItem.actual_set_id == existing_set.id,
                        )
                        .order_by(PayrollContributionActualItem.id)
                    )
                ],
                "idempotent_replay": True,
            }
        employee = self._employee_for_org(request.org_id, request.employee_id)
        if employee is None:
            return {"status": "rejected", "errors": ["EMPLOYEE_NOT_FOUND"]}
        try:
            evidence_ids = self._validate_payroll_batch_evidence(
                request.org_id, request.evidence_references
            )
        except CalculationValidationError as exc:
            return {"status": "rejected", "errors": [exc.code]}
        period = YearMonth(
            int(request.contribution_period[:4]), int(request.contribution_period[5:])
        )
        policy_record = self._effective_payroll_policy(request.org_id, period.end_date)
        if policy_record is None:
            return {
                "status": "needs_information",
                "missing_information": [
                    {
                        "code": "payroll_policy",
                        "message": "the contribution period needs an effective company policy",
                        "fields": ["contribution_policy_version"],
                    }
                ],
            }
        try:
            contribution_policy, _, _ = self._calculator_policies(policy_record)
        except CalculationValidationError as exc:
            return {"status": "rejected", "errors": [f"{exc.code}:{exc}"]}
        policy_keys = {(str(rule.base_kind), rule.code) for rule in contribution_policy.rules}
        requested_by_key = {
            (item.contribution_group.value, item.insurance_kind): item for item in request.items
        }
        unknown_keys = sorted(set(requested_by_key).difference(policy_keys))
        if unknown_keys:
            return {
                "status": "rejected",
                "errors": ["CONTRIBUTION_ACTUAL_KIND_NOT_IN_POLICY"],
                "invalid_items": [f"{group}:{kind}" for group, kind in unknown_keys],
            }
        active_items = self._active_contribution_actual_items(
            request.org_id, request.employee_id, request.contribution_period
        )
        active_by_key = {
            (item.contribution_group, item.insurance_kind): item for item in active_items
        }
        supersedes_ids = set(request.supersedes_actual_ids)
        supplied_predecessors = list(
            self.session.scalars(
                select(PayrollContributionActualItem)
                .where(
                    PayrollContributionActualItem.org_id == request.org_id,
                    PayrollContributionActualItem.id.in_(supersedes_ids),
                )
                .with_for_update()
            )
        ) if supersedes_ids else []
        if {item.id for item in supplied_predecessors} != supersedes_ids:
            return {"status": "rejected", "errors": ["INVALID_CONTRIBUTION_ACTUAL_SUPERSEDES"]}
        supplied_by_key = {
            (item.contribution_group, item.insurance_kind): item for item in supplied_predecessors
        }
        if any(
            item.employee_id != request.employee_id
            or item.contribution_period != request.contribution_period
            for item in supplied_predecessors
        ) or set(supplied_by_key) != {
            key for key in requested_by_key if key in active_by_key
        }:
            return {"status": "rejected", "errors": ["INVALID_CONTRIBUTION_ACTUAL_SUPERSEDES"]}
        for key, active in active_by_key.items():
            supplied = supplied_by_key.get(key)
            if key in requested_by_key and (supplied is None or supplied.id != active.id):
                return {
                    "status": "rejected",
                    "errors": ["CONTRIBUTION_ACTUAL_CORRECTION_REQUIRES_SUPERSEDES"],
                }
        calculated_batches_to_supersede: list[PayrollBatch] = []
        if supersedes_ids:
            use_rows = self.session.execute(
                select(PayrollContributionActualUse, PayrollBatch)
                .join(
                    PayrollBatch,
                    (PayrollBatch.org_id == PayrollContributionActualUse.org_id)
                    & (PayrollBatch.id == PayrollContributionActualUse.payroll_batch_id),
                )
                .where(
                    PayrollContributionActualUse.org_id == request.org_id,
                    PayrollContributionActualUse.actual_item_id.in_(supersedes_ids),
                    PayrollBatch.status.in_(("calculated", "posted")),
                )
                .with_for_update()
            ).all()
            posted_batches = sorted(
                {str(batch.id) for _, batch in use_rows if batch.status == "posted"}
            )
            if posted_batches:
                return {
                    "status": "rejected",
                    "errors": ["CONTRIBUTION_ACTUAL_POSTED_PAYROLL_MUST_BE_REVERSED_FIRST"],
                    "blocking_payroll_batch_ids": posted_batches,
                }
            calculated_batches_to_supersede = [batch for _, batch in use_rows]

        actual_set = PayrollContributionActualSet(
            org_id=request.org_id,
            employee_id=request.employee_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            contribution_period=request.contribution_period,
            declaration_date=request.declaration_date,
            reason_code=request.reason_code,
            reason_description=request.reason_description,
        )
        try:
            with self.session.begin_nested():
                for batch in calculated_batches_to_supersede:
                    batch.status = "superseded"
                self.session.add(actual_set)
                self.session.flush()
                new_items: list[PayrollContributionActualItem] = []
                for key, request_item in requested_by_key.items():
                    predecessor = supplied_by_key.get(key)
                    item = PayrollContributionActualItem(
                        org_id=request.org_id,
                        actual_set_id=actual_set.id,
                        employee_id=request.employee_id,
                        contribution_period=request.contribution_period,
                        contribution_group=key[0],
                        insurance_kind=key[1],
                        actual_state=request_item.actual_state.value,
                        employee_amount_fen=request_item.employee_amount_fen,
                        employer_amount_fen=request_item.employer_amount_fen,
                        supersedes_id=predecessor.id if predecessor is not None else None,
                    )
                    self.session.add(item)
                    new_items.append(item)
                for evidence_id in evidence_ids:
                    self.session.add(
                        PayrollContributionActualEvidence(
                            org_id=request.org_id,
                            actual_set_id=actual_set.id,
                            evidence_id=evidence_id,
                        )
                    )
                self.session.flush()
        except IntegrityError:
            concurrent = self.session.scalar(
                select(PayrollContributionActualSet).where(
                    PayrollContributionActualSet.org_id == request.org_id,
                    PayrollContributionActualSet.idempotency_key == request.idempotency_key,
                )
            )
            if concurrent is not None and concurrent.request_payload_hash == payload_hash:
                return {
                    **self._contribution_actual_result(concurrent),
                    "idempotent_replay": True,
                }
            return {
                "status": "rejected",
                "errors": ["PAYROLL_CONTRIBUTION_ACTUAL_CONCURRENT_WRITE_CONFLICT"],
            }
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                action="payroll_contribution_actual_registered",
                details={
                    "actual_set_id": str(actual_set.id),
                    "employee_id": str(request.employee_id),
                    "contribution_period": request.contribution_period,
                    "actual_item_ids": [str(item.id) for item in new_items],
                    "supersedes_actual_ids": [str(value) for value in supersedes_ids],
                },
            )
        )
        return {
            **self._contribution_actual_result(actual_set),
            "actual_item_ids": [str(item.id) for item in new_items],
        }

    def record_payroll_contribution_supplement(
        self, request: RecordPayrollContributionSupplementRequest
    ) -> FinanceResult:
        """Post a historical assessment in the current open period using a fixed template."""

        payload_hash = self._request_payload_hash(request)
        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if error := self._idempotency_error(existing, payload_hash, payroll_envelope=True):
                return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
            return self._result_for_existing(existing)
        employee = self._employee_for_org(request.org_id, request.employee_id)
        if employee is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["EMPLOYEE_NOT_FOUND"])
        try:
            evidence_ids = self._validate_payroll_batch_evidence(
                request.org_id, request.evidence_references
            )
            assert_period_open(self.session, request.org_id, request.posting_date)
        except CalculationValidationError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[exc.code])
        except AccountingPeriodError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[exc.code])
        period = YearMonth(
            int(request.contribution_period[:4]), int(request.contribution_period[5:])
        )
        policy_record = self._effective_payroll_policy(request.org_id, period.end_date)
        if policy_record is None:
            return FinanceResult(
                status=ResultStatus.NEEDS_INFORMATION,
                missing_information=["contribution_policy_version"],
            )
        contribution_policy, _, _ = self._calculator_policies(policy_record)
        policy_keys = {(str(rule.base_kind), rule.code) for rule in contribution_policy.rules}
        item_keys = {(item.contribution_group.value, item.insurance_kind) for item in request.items}
        if invalid := sorted(item_keys.difference(policy_keys)):
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=[
                    "CONTRIBUTION_SUPPLEMENT_KIND_NOT_IN_POLICY:"
                    + ",".join(f"{group}:{kind}" for group, kind in invalid)
                ],
            )
        profile = self._effective_profile(employee.id, period.end_date)
        if profile is None:
            return FinanceResult(
                status=ResultStatus.NEEDS_INFORMATION,
                missing_information=["employee_payroll_profile_version"],
            )
        source_batches = self.session.scalars(
            select(PayrollBatch)
            .join(
                PayrollLine,
                (PayrollLine.org_id == PayrollBatch.org_id)
                & (PayrollLine.payroll_batch_id == PayrollBatch.id),
            )
            .where(
                PayrollBatch.org_id == request.org_id,
                PayrollBatch.batch_kind == PayrollBatchKind.REGULAR.value,
                PayrollBatch.payroll_period == request.contribution_period,
                PayrollBatch.status == "posted",
                PayrollLine.employee_id == request.employee_id,
            )
            .order_by(PayrollBatch.id)
        ).all()
        if len(source_batches) != 1:
            return FinanceResult(
                status=ResultStatus.NEEDS_INFORMATION,
                missing_information=["unique_posted_source_payroll_batch"],
            )
        source_batch = source_batches[0]
        duplicate_assessment = self.session.scalar(
            select(PayrollContributionSupplement.id).where(
                PayrollContributionSupplement.org_id == request.org_id,
                PayrollContributionSupplement.employee_id == request.employee_id,
                PayrollContributionSupplement.assessment_reference == request.assessment_reference,
            )
        )
        if duplicate_assessment is not None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["CONTRIBUTION_SUPPLEMENT_ASSESSMENT_ALREADY_RECORDED"],
            )
        targets = self._payment_targets(policy_record.parameters)
        entries: list[Entry] = []
        plans: list[OpenItemPlan] = []
        item_trace: list[dict[str, Any]] = []
        for item in request.items:
            is_social = item.contribution_group.value == "social_insurance"
            employer_role = (
                "employer_social_payable" if is_social else "employer_housing_fund_payable"
            )
            withheld_role = (
                "withheld_employee_social_payable"
                if is_social
                else "withheld_employee_housing_fund_payable"
            )
            employer_category = "employer_social" if is_social else "employer_housing"
            withheld_category = (
                "withheld_employee_social" if is_social else "withheld_employee_housing"
            )
            target = targets["social_insurance" if is_social else "housing_fund"]
            agency = self._agency_counterparty(request.org_id, target)
            employer_borne_employee = (
                item.employee_amount_fen
                if item.employee_amount_treatment == "employer_borne"
                else 0
            )
            employer_payable = item.employer_amount_fen + employer_borne_employee
            if employer_payable:
                entries.extend(
                    [
                        Entry(
                            account_role=profile.expense_role,
                            debit_fen=employer_payable,
                            counterparty_id=employee.counterparty_id,
                        ),
                        Entry(account_role=employer_role, credit_fen=employer_payable),
                    ]
                )
                plans.append(
                    OpenItemPlan(
                        counterparty_id=agency.id,
                        item_type="payable",
                        original_amount_fen=employer_payable,
                        due_date=request.due_date,
                        payable_category=employer_category,
                        payable_agency_code=target["agency_code"],
                        insurance_kind=item.insurance_kind,
                    )
                )
            employee_receivable = (
                item.employee_amount_fen
                if item.employee_amount_treatment == "employee_receivable"
                else 0
            )
            if employee_receivable:
                entries.extend(
                    [
                        Entry(
                            account_role="employee_receivable",
                            debit_fen=employee_receivable,
                            counterparty_id=employee.counterparty_id,
                        ),
                        Entry(account_role=withheld_role, credit_fen=employee_receivable),
                    ]
                )
                plans.extend(
                    [
                        OpenItemPlan(
                            counterparty_id=employee.counterparty_id,
                            item_type="receivable",
                            original_amount_fen=employee_receivable,
                            due_date=request.due_date,
                        ),
                        OpenItemPlan(
                            counterparty_id=agency.id,
                            item_type="payable",
                            original_amount_fen=employee_receivable,
                            due_date=request.due_date,
                            payable_category=withheld_category,
                            payable_agency_code=target["agency_code"],
                            insurance_kind=item.insurance_kind,
                        ),
                    ]
                )
            item_trace.append(
                {
                    "contribution_group": item.contribution_group.value,
                    "insurance_kind": item.insurance_kind,
                    "employee_amount_fen": item.employee_amount_fen,
                    "employer_amount_fen": item.employer_amount_fen,
                    "employee_amount_treatment": item.employee_amount_treatment,
                }
            )
        event = BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            event_type="payroll_contribution_supplement",
            status="draft",
            description=(
                f"{request.contribution_period} 社保公积金历史补缴确认："
                f"{request.reason_description}"
            ),
            facts={
                "employee_id": str(employee.id),
                "contribution_period": request.contribution_period,
                "assessment_reference": request.assessment_reference,
                "reason_code": request.reason_code,
                "items": item_trace,
            },
            business_date=request.posting_date,
            posting_date=request.posting_date,
            rule_trace=[
                {
                    "stage": "payroll_contribution_supplement_template",
                    "policy_version_id": str(policy_record.id),
                    "contribution_period": request.contribution_period,
                    "posting_date": request.posting_date.isoformat(),
                    "items": item_trace,
                }
            ],
            rule_version=policy_record.version,
        )
        try:
            with self.session.begin_nested():
                self.session.add(event)
                self.session.flush()
                self._attach_evidence(event, evidence_ids)
                voucher = create_voucher(
                    self.session,
                    event=event,
                    posting_date=request.posting_date,
                    description=event.description,
                    entries=entries,
                )
                create_open_items(self.session, event=event, plans=plans)
                supplement = PayrollContributionSupplement(
                    org_id=request.org_id,
                    event_id=event.id,
                    employee_id=employee.id,
                    source_payroll_batch_id=source_batch.id,
                    contribution_period=request.contribution_period,
                    assessment_reference=request.assessment_reference,
                    reason_code=request.reason_code,
                    reason_description=request.reason_description,
                )
                self.session.add(supplement)
                self.session.add(
                    PayrollEventLink(
                        org_id=request.org_id,
                        event_id=event.id,
                        payroll_batch_id=source_batch.id,
                        link_kind="contribution_supplement",
                    )
                )
                self.session.flush()
                for item in request.items:
                    self.session.add(
                        PayrollContributionSupplementItem(
                            org_id=request.org_id,
                            supplement_id=supplement.id,
                            contribution_group=item.contribution_group.value,
                            insurance_kind=item.insurance_kind,
                            employee_amount_fen=item.employee_amount_fen,
                            employer_amount_fen=item.employer_amount_fen,
                            employee_amount_treatment=item.employee_amount_treatment,
                        )
                    )
                # PostgreSQL treats the final event transition as the seal for
                # the complete normalized supplement and evidence graph.
                self.session.flush()
                event.status = "posted"
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        event_id=event.id,
                        action="payroll_contribution_supplement_posted",
                        details={
                            "supplement_id": str(supplement.id),
                            "employee_id": str(employee.id),
                            "contribution_period": request.contribution_period,
                        },
                    )
                )
                self.session.flush()
        except IntegrityError:
            concurrent = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if concurrent is not None and concurrent.request_payload_hash == payload_hash:
                return self._result_for_existing(concurrent)
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["CONTRIBUTION_SUPPLEMENT_CONCURRENT_WRITE_CONFLICT"],
            )
        return FinanceResult(
            status=ResultStatus.POSTED,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            trace=event.rule_trace,
            data={"supplement_id": str(supplement.id)},
        )

    def preview_payroll(self, request: PreviewPayrollRequest) -> PayrollResult:
        if self.session.get(Organization, request.org_id) is None:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED, errors=["ORGANIZATION_NOT_FOUND"]
            )
        payload_hash = self._preview_request_payload_hash(request)
        existing = self.session.scalar(
            select(PayrollBatch).where(
                PayrollBatch.org_id == request.org_id,
                PayrollBatch.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            existing_payload_hash = existing.request_payload_hash
            if existing_payload_hash is None:
                stored_request = existing.calculation_input.get("request")
                if isinstance(stored_request, dict):
                    existing_payload_hash = self._canonical_payload_hash(stored_request)
            if existing_payload_hash == payload_hash:
                return self._payroll_result_for_batch(existing, idempotent_replay=True)
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"],
            )
        try:
            self._validate_payroll_batch_evidence(request.org_id, request.evidence_references)
        except CalculationValidationError as exc:
            return PayrollResult(status=PayrollResultStatus.REJECTED, errors=[exc.code])
        try:
            calculation = self._calculate_payroll(request)
        except NeedsInformationError as exc:
            return PayrollResult(
                status=PayrollResultStatus.NEEDS_INFORMATION,
                missing_information=[item.as_dict() for item in exc.requirements],
            )
        except CalculationValidationError as exc:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=[f"{exc.code}:{exc}"],
            )

        if calculation["missing"]:
            return PayrollResult(
                status=PayrollResultStatus.NEEDS_INFORMATION,
                missing_information=calculation["missing"],
                data={"annual_bonus_scenarios": calculation["scenarios"]},
            )
        try:
            self._lock_tax_period_org(request.org_id)
            assert_period_open(self.session, request.org_id, request.posting_date)
        except AccountingPeriodError as exc:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=[exc.code],
            )
        try:
            with self.session.begin_nested():
                version = self._allocate_payroll_batch_version(
                    request.org_id,
                    request.batch_kind.value,
                    request.payroll_period,
                )
                self.session.execute(
                    update(PayrollBatch)
                    .where(
                        PayrollBatch.org_id == request.org_id,
                        PayrollBatch.batch_kind == request.batch_kind.value,
                        PayrollBatch.payroll_period == request.payroll_period,
                        PayrollBatch.status == "calculated",
                    )
                    .values(status="superseded"),
                    execution_options={"synchronize_session": "fetch"},
                )
                batch = PayrollBatch(
                    org_id=request.org_id,
                    idempotency_key=request.idempotency_key,
                    batch_kind=request.batch_kind.value,
                    payroll_period=request.payroll_period,
                    version=version,
                    # Evidence and payroll lines are written only while the
                    # database-recognized draft is mutable; one final
                    # transition seals the complete calculation evidence set.
                    status="draft",
                    calculation_hash=calculation["calculation_hash"],
                    request_payload_hash=payload_hash,
                    calculation_input=calculation["calculation_input"],
                    calculation_trace=calculation["trace"],
                    policy_snapshot=calculation["policy_snapshot"],
                    policy_version_id=calculation["policy"].id,
                    posting_date=request.posting_date,
                    payment_date=request.payment_date,
                    tax_method=request.tax_method.value if request.tax_method else None,
                )
                self.session.add(batch)
                self.session.flush()
                self._attach_payroll_batch_evidence(batch, request.evidence_references)
                for actual_item_id in calculation["actual_item_ids"]:
                    self.session.add(
                        PayrollContributionActualUse(
                            org_id=request.org_id,
                            actual_item_id=actual_item_id,
                            payroll_batch_id=batch.id,
                        )
                    )
                for treatment_id in calculation["first_wage_treatment_ids"]:
                    self.session.add(
                        PayrollFirstWageTaxTreatmentUse(
                            org_id=request.org_id,
                            treatment_id=treatment_id,
                            payroll_batch_id=batch.id,
                        )
                    )
                for prepared in calculation["lines"]:
                    self.session.add(
                        PayrollLine(org_id=request.org_id, payroll_batch_id=batch.id, **prepared)
                    )
                # PostgreSQL freezes evidence edges as soon as the batch is
                # sealed.  Flush every draft-only edge before transitioning
                # the parent row, otherwise SQLAlchemy may issue the status
                # UPDATE before the association INSERT in one flush.
                self.session.flush()
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        action="payroll_calculated",
                        details={
                            "batch_id": str(batch.id),
                            "calculation_hash": batch.calculation_hash,
                        },
                    )
                )
                batch.status = "calculated"
                self.session.flush()
                self._assert_unfinished_payroll_period_constraint_now()
        except IntegrityError:
            existing = self.session.scalar(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == request.org_id,
                    PayrollBatch.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                existing_payload_hash = existing.request_payload_hash
                if existing_payload_hash is None:
                    stored_request = existing.calculation_input.get("request")
                    if isinstance(stored_request, dict):
                        existing_payload_hash = self._canonical_payload_hash(stored_request)
                if existing_payload_hash == payload_hash:
                    return self._payroll_result_for_batch(existing, idempotent_replay=True)
                return PayrollResult(
                    status=PayrollResultStatus.REJECTED,
                    errors=["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )
        except DBAPIError as exc:
            if code := self._accounting_period_database_error_code(exc):
                return PayrollResult(
                    status=PayrollResultStatus.REJECTED,
                    errors=[code],
                )
            raise
        return self._payroll_result_for_batch(batch)

    def confirm_payroll(self, request: ConfirmPayrollRequest) -> PayrollResult:
        """Confirm through the common payroll idempotency/savepoint envelope."""

        payload_hash = self._request_payload_hash(request)
        try:
            with self.session.begin_nested():
                result = self._confirm_payroll_write(request)
                if result.status == PayrollResultStatus.POSTED:
                    self._assert_round6_final_dependency_constraints_now()
                return result
        except AccountingPeriodError as exc:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=[exc.code],
            )
        except IntegrityError:
            existing = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                if error := self._idempotency_error(existing, payload_hash, payroll_envelope=True):
                    return PayrollResult(status=PayrollResultStatus.REJECTED, errors=[error])
                if existing.event_type == "payroll_accrual":
                    linked_batch_id = self.session.scalar(
                        select(PayrollEventLink.payroll_batch_id).where(
                            PayrollEventLink.org_id == request.org_id,
                            PayrollEventLink.event_id == existing.id,
                            PayrollEventLink.link_kind == "payroll_accrual",
                        )
                    )
                    batch = self.session.get(PayrollBatch, request.batch_id)
                    if batch is not None and linked_batch_id == batch.id:
                        return self._payroll_result_for_batch(batch, idempotent_replay=True)
                return PayrollResult(
                    status=PayrollResultStatus.REJECTED,
                    errors=["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )
        except DBAPIError as exc:
            if self._is_round6_final_dependency_error(exc):
                return PayrollResult(
                    status=PayrollResultStatus.REJECTED,
                    errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
                )
            raise

    def _confirm_payroll_write(self, request: ConfirmPayrollRequest) -> PayrollResult:
        batch = self.session.scalar(
            select(PayrollBatch)
            .where(PayrollBatch.id == request.batch_id, PayrollBatch.org_id == request.org_id)
            .with_for_update()
        )
        if batch is None:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED, errors=["PAYROLL_BATCH_NOT_FOUND"]
            )
        confirm_payload_hash = self._request_payload_hash(request)
        existing_event = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing_event is not None:
            if error := self._idempotency_error(
                existing_event, confirm_payload_hash, payroll_envelope=True
            ):
                return PayrollResult(status=PayrollResultStatus.REJECTED, errors=[error])
            if existing_event.event_type == "payroll_accrual":
                linked_batch = self.session.scalar(
                    select(PayrollEventLink.payroll_batch_id).where(
                        PayrollEventLink.org_id == request.org_id,
                        PayrollEventLink.event_id == existing_event.id,
                        PayrollEventLink.link_kind == "payroll_accrual",
                    )
                )
                if linked_batch == batch.id:
                    return self._payroll_result_for_batch(batch, idempotent_replay=True)
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"],
            )
        if batch.status != "calculated":
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["PAYROLL_BATCH_IS_NOT_CONFIRMABLE"],
            )
        if batch.calculation_hash != request.calculation_hash:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["STALE_PAYROLL_CALCULATION"],
            )
        if batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value and batch.tax_method is None:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["ANNUAL_BONUS_TAX_METHOD_REQUIRED"],
            )
        evidence_ids = self.session.scalars(
            select(PayrollBatchEvidence.evidence_id)
            .where(
                PayrollBatchEvidence.org_id == batch.org_id,
                PayrollBatchEvidence.payroll_batch_id == batch.id,
            )
            .order_by(PayrollBatchEvidence.evidence_id)
        ).all()
        requested_evidence_ids = [
            uuid.UUID(value)
            for value in batch.calculation_input.get("request", {}).get("evidence_references", [])
        ]
        if sorted(evidence_ids) != sorted(requested_evidence_ids):
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["STALE_PAYROLL_CALCULATION"],
            )
        stored_actual_item_ids = set(
            self.session.scalars(
                select(PayrollContributionActualUse.actual_item_id).where(
                    PayrollContributionActualUse.org_id == batch.org_id,
                    PayrollContributionActualUse.payroll_batch_id == batch.id,
                )
            )
        )
        stored_first_wage_treatment_ids = set(
            self.session.scalars(
                select(PayrollFirstWageTaxTreatmentUse.treatment_id).where(
                    PayrollFirstWageTaxTreatmentUse.org_id == batch.org_id,
                    PayrollFirstWageTaxTreatmentUse.payroll_batch_id == batch.id,
                )
            )
        )
        lines = self.session.scalars(
            select(PayrollLine)
            .where(PayrollLine.payroll_batch_id == batch.id)
            .order_by(PayrollLine.id)
        ).all()
        if not lines:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["STALE_PAYROLL_CALCULATION"],
            )
        # Lock the persistent annual domain *before* re-reading cumulative
        # state.  Otherwise a January and March batch both calculated from an
        # empty slot range can independently confirm.
        tax_state_lines = [
            line for line in lines if self._line_uses_cumulative_tax_state(batch, line)
        ]
        if tax_state_lines:
            try:
                batch_tax_period = self._batch_tax_period(batch)
                self._lock_payroll_tax_year(
                    batch.org_id,
                    [line.employee_id for line in tax_state_lines],
                    batch_tax_period.year,
                )
            except CalculationValidationError as exc:
                return PayrollResult(status=PayrollResultStatus.REJECTED, errors=[exc.code])
        try:
            stored_request = PreviewPayrollRequest.model_validate(
                batch.calculation_input["request"]
            )
            recalculated = self._calculate_payroll(stored_request)
        except (KeyError, NeedsInformationError, CalculationValidationError, ValueError):
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["STALE_PAYROLL_CALCULATION"],
            )
        if (
            recalculated["missing"]
            or recalculated["calculation_hash"] != batch.calculation_hash
            or recalculated["policy_snapshot"] != batch.policy_snapshot
            or set(recalculated["actual_item_ids"]) != stored_actual_item_ids
            or set(recalculated["first_wage_treatment_ids"])
            != stored_first_wage_treatment_ids
        ):
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=["STALE_PAYROLL_CALCULATION"],
            )
        tax_state_savepoint = self.session.begin_nested()
        try:
            self._reserve_payroll_tax_state_slots(batch, lines)
        except CalculationValidationError as exc:
            tax_state_savepoint.rollback()
            return PayrollResult(status=PayrollResultStatus.REJECTED, errors=[exc.code])
        else:
            tax_state_savepoint.commit()
        self._create_payroll_withholding_entitlements(batch, lines)
        entries, open_item_plans = self._payroll_accrual_template(batch, lines)
        event = BusinessEvent(
            org_id=batch.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=confirm_payload_hash,
            event_type="payroll_accrual",
            # R3 final-event guards require a complete voucher, evidence and
            # normalized source edge before an event becomes final.
            status="draft",
            description=batch.calculation_input["request"].get("description") or "工资计提",
            facts={
                "payroll_batch_id": str(batch.id),
                "calculation_hash": batch.calculation_hash,
                "payment_date": batch.payment_date.isoformat(),
            },
            business_date=batch.posting_date,
            posting_date=batch.posting_date,
            rule_trace=[
                *batch.calculation_trace,
                {
                    "stage": "payroll_accrual_template",
                    "debit_fen": sum(entry.debit_fen for entry in entries),
                    "credit_fen": sum(entry.credit_fen for entry in entries),
                    "open_item_count": len(open_item_plans),
                },
            ],
            rule_version=batch.policy_snapshot.get("version"),
        )
        self.session.add(event)
        self.session.flush()
        self.session.add(
            PayrollEventLink(
                org_id=batch.org_id,
                event_id=event.id,
                payroll_batch_id=batch.id,
                link_kind="payroll_accrual",
            )
        )
        self._attach_evidence(event, evidence_ids)
        # Final source and evidence edges must be physically present before
        # the one-way draft -> posted transition below.
        self.session.flush()
        create_voucher(
            self.session,
            event=event,
            posting_date=batch.posting_date,
            description=event.description,
            entries=entries,
        )
        create_open_items(self.session, event=event, plans=open_item_plans)
        event.status = "posted"
        batch.status = "posted"
        batch.business_event_id = event.id
        batch.confirmed_by = None
        batch.confirmation_note = request.confirmation_note
        batch.confirmed_at = datetime.now(UTC)
        if (
            batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value
            and batch.tax_method == "separate"
        ):
            for line in lines:
                self.session.add(
                    AnnualBonusUsage(
                        org_id=batch.org_id,
                        employee_id=line.employee_id,
                        tax_year=self._batch_tax_period(batch).year,
                        payroll_batch_id=batch.id,
                        payroll_line_id=line.id,
                    )
                )
        self.session.add(
            AuditLog(
                org_id=batch.org_id,
                event_id=event.id,
                action="payroll_confirmed",
                actor="ai_agent:ai-accounting-core",
                details={"batch_id": str(batch.id), "calculation_hash": batch.calculation_hash},
            )
        )
        self.session.flush()
        return self._payroll_result_for_batch(batch)

    def get_payroll_batch(self, org_id: uuid.UUID, batch_id: uuid.UUID) -> dict[str, Any]:
        batch = self.session.scalar(
            select(PayrollBatch).where(PayrollBatch.org_id == org_id, PayrollBatch.id == batch_id)
        )
        if batch is None:
            return {"status": "rejected", "errors": ["PAYROLL_BATCH_NOT_FOUND"]}
        result = self._payroll_result_for_batch(batch)
        return {
            "status": "ok",
            **result.model_dump(mode="json"),
            "lifecycle": self._payroll_batch_lifecycle(org_id, batch),
        }

    def _payroll_batch_lifecycle(self, org_id: uuid.UUID, batch: PayrollBatch) -> dict[str, Any]:
        """Return a stable, organization-scoped payroll evidence graph."""
        event_ids = {batch.business_event_id} if batch.business_event_id else set()
        open_items_by_id: dict[uuid.UUID, OpenItem] = {}
        settlements_by_id: dict[uuid.UUID, Settlement] = {}
        queried_event_ids: set[uuid.UUID] = set()
        for _ in range(32):
            source_event_ids = event_ids - queried_event_ids
            if not source_event_ids:
                break
            queried_event_ids.update(source_event_ids)
            source_items = self.session.scalars(
                select(OpenItem)
                .where(
                    OpenItem.org_id == org_id,
                    OpenItem.source_event_id.in_(source_event_ids),
                )
                .order_by(OpenItem.id)
            ).all()
            open_items_by_id.update({item.id: item for item in source_items})
            if not source_items:
                continue
            settlements = self.session.scalars(
                select(Settlement)
                .where(
                    Settlement.org_id == org_id,
                    Settlement.open_item_id.in_([item.id for item in source_items]),
                )
                .order_by(Settlement.id)
            ).all()
            settlements_by_id.update({settlement.id: settlement for settlement in settlements})
            event_ids.update(settlement.payment_event_id for settlement in settlements)

        events: list[BusinessEvent] = []
        for _ in range(8):
            events = (
                self.session.scalars(
                    select(BusinessEvent)
                    .where(BusinessEvent.org_id == org_id, BusinessEvent.id.in_(event_ids))
                    .order_by(BusinessEvent.posting_date, BusinessEvent.id)
                ).all()
                if event_ids
                else []
            )
            reversal_event_ids = {
                event.reversed_by_event_id
                for event in events
                if event.reversed_by_event_id is not None
            }
            if reversal_event_ids <= event_ids:
                break
            event_ids.update(reversal_event_ids)
        reversal_parent_by_id = {
            event.reversed_by_event_id: event.id
            for event in events
            if event.reversed_by_event_id is not None
        }
        event_evidence_rows = (
            self.session.execute(
                select(
                    event_evidence.c.event_id,
                    event_evidence.c.evidence_id,
                    event_evidence.c.relation_kind,
                )
                .where(
                    event_evidence.c.org_id == org_id,
                    event_evidence.c.event_id.in_([event.id for event in events]),
                )
                .order_by(
                    event_evidence.c.event_id,
                    event_evidence.c.relation_kind,
                    event_evidence.c.evidence_id,
                )
            ).all()
            if events
            else []
        )
        vouchers = (
            self.session.scalars(
                select(Voucher)
                .where(
                    Voucher.org_id == org_id, Voucher.event_id.in_([event.id for event in events])
                )
                .order_by(Voucher.posting_date, Voucher.id)
            ).all()
            if events
            else []
        )
        lines = self.session.scalars(
            select(PayrollLine)
            .where(PayrollLine.org_id == org_id, PayrollLine.payroll_batch_id == batch.id)
            .order_by(PayrollLine.id)
        ).all()
        profile_ids = [line.employee_payroll_profile_version_id for line in lines]
        profiles = (
            {
                profile.id: profile
                for profile in self.session.scalars(
                    select(EmployeePayrollProfileVersion).where(
                        EmployeePayrollProfileVersion.org_id == org_id,
                        EmployeePayrollProfileVersion.id.in_(profile_ids),
                    )
                ).all()
            }
            if profile_ids
            else {}
        )
        employees = (
            {
                employee.id: employee
                for employee in self.session.scalars(
                    select(Employee).where(
                        Employee.org_id == org_id,
                        Employee.id.in_([line.employee_id for line in lines]),
                    )
                ).all()
            }
            if lines
            else {}
        )
        policy = self.session.scalar(
            select(PayrollPolicyVersion).where(
                PayrollPolicyVersion.org_id == org_id,
                PayrollPolicyVersion.id == batch.policy_version_id,
            )
        )
        evidence_ids = self.session.scalars(
            select(PayrollBatchEvidence.evidence_id)
            .where(
                PayrollBatchEvidence.org_id == org_id,
                PayrollBatchEvidence.payroll_batch_id == batch.id,
            )
            .order_by(PayrollBatchEvidence.evidence_id)
        ).all()
        evidence = (
            self.session.scalars(
                select(Evidence)
                .where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
                .order_by(Evidence.id)
            ).all()
            if evidence_ids
            else []
        )
        payment_event_types = {
            EventType.SALARY_PAYMENT.value,
            EventType.SOCIAL_INSURANCE_PAYMENT.value,
            EventType.HOUSING_FUND_PAYMENT.value,
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT.value,
        }
        payment_events = [event for event in events if event.event_type in payment_event_types]
        payroll_event_links = (
            self.session.scalars(
                select(PayrollEventLink)
                .where(
                    PayrollEventLink.org_id == org_id,
                    PayrollEventLink.event_id.in_([event.id for event in events]),
                )
                .order_by(
                    PayrollEventLink.event_id,
                    PayrollEventLink.link_kind,
                    PayrollEventLink.source_payment_event_id,
                    PayrollEventLink.source_open_item_id,
                    PayrollEventLink.id,
                )
            ).all()
            if events
            else []
        )
        linked_batch_ids = {link.payroll_batch_id for link in payroll_event_links}
        linked_batches = (
            {
                linked_batch.id: linked_batch
                for linked_batch in self.session.scalars(
                    select(PayrollBatch).where(
                        PayrollBatch.org_id == org_id,
                        PayrollBatch.id.in_(linked_batch_ids),
                    )
                ).all()
            }
            if linked_batch_ids
            else {}
        )
        link_source_item_ids = {
            link.source_open_item_id
            for link in payroll_event_links
            if link.source_open_item_id is not None
        }
        link_source_items = (
            {
                item.id: item
                for item in self.session.scalars(
                    select(OpenItem).where(
                        OpenItem.org_id == org_id,
                        OpenItem.id.in_(link_source_item_ids),
                    )
                ).all()
            }
            if link_source_item_ids
            else {}
        )
        link_source_event_ids = {
            link.source_payment_event_id
            for link in payroll_event_links
            if link.source_payment_event_id is not None
        }
        link_source_events = (
            {
                source_event.id: source_event
                for source_event in self.session.scalars(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == org_id,
                        BusinessEvent.id.in_(link_source_event_ids),
                    )
                ).all()
            }
            if link_source_event_ids
            else {}
        )
        payroll_links_by_event_id: dict[uuid.UUID, list[PayrollEventLink]] = {}
        for link in payroll_event_links:
            payroll_links_by_event_id.setdefault(link.event_id, []).append(link)
        bank_matches = (
            self.session.scalars(
                select(BankTransactionMatch)
                .where(
                    BankTransactionMatch.org_id == org_id,
                    (
                        BankTransactionMatch.event_id.in_([event.id for event in events])
                        | BankTransactionMatch.invalidated_by_event_id.in_(
                            [event.id for event in events]
                        )
                    ),
                )
                .order_by(BankTransactionMatch.created_at, BankTransactionMatch.id)
            ).all()
            if events
            else []
        )
        bank_by_id = {
            row.id: row
            for row in self.session.scalars(
                select(BankTransaction).where(
                    BankTransaction.org_id == org_id,
                    BankTransaction.id.in_([match.bank_transaction_id for match in bank_matches]),
                )
            ).all()
        }
        matched_bank = (
            self.session.scalars(
                select(BankTransaction)
                .where(
                    BankTransaction.org_id == org_id,
                    BankTransaction.matched_event_id.in_([event.id for event in payment_events]),
                )
                .order_by(BankTransaction.booking_date, BankTransaction.id)
            ).all()
            if payment_events
            else []
        )
        reversal_batches = self.session.scalars(
            select(PayrollBatch)
            .where(
                PayrollBatch.org_id == org_id,
                (PayrollBatch.id == batch.id) | (PayrollBatch.reversal_of_batch_id == batch.id),
            )
            .order_by(PayrollBatch.version, PayrollBatch.id)
        ).all()
        audit_logs = self.session.scalars(
            select(AuditLog)
            .where(AuditLog.org_id == org_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
        related_audit_logs = [
            log
            for log in audit_logs
            if log.event_id in event_ids or log.details.get("batch_id") == str(batch.id)
        ]
        request = batch.calculation_input.get("request", {})
        employee_items = request.get("employee_items", []) if isinstance(request, dict) else []
        sources = self._payroll_policy_sources(policy, batch.policy_snapshot)

        return {
            "calculation": {
                "calculation_hash": batch.calculation_hash,
                "request_payload_hash": batch.request_payload_hash,
                "input_summary": {
                    "batch_kind": request.get("batch_kind") if isinstance(request, dict) else None,
                    "payroll_period": request.get("payroll_period")
                    if isinstance(request, dict)
                    else None,
                    "posting_date": request.get("posting_date")
                    if isinstance(request, dict)
                    else None,
                    "payment_date": request.get("payment_date")
                    if isinstance(request, dict)
                    else None,
                    "employee_count": len(employee_items),
                    "employee_ids": sorted(
                        str(item["employee_id"])
                        for item in employee_items
                        if isinstance(item, dict) and item.get("employee_id")
                    ),
                    "evidence_reference_count": len(evidence_ids),
                },
                "trace": batch.calculation_trace,
            },
            "employee_snapshots": [
                {
                    "employee_id": str(line.employee_id),
                    "employee_code": employees[line.employee_id].employee_code
                    if line.employee_id in employees
                    else None,
                    "profile_version_id": str(line.employee_payroll_profile_version_id),
                    "profile": {
                        "effective_from": profiles[
                            line.employee_payroll_profile_version_id
                        ].effective_from.isoformat(),
                        "effective_to": (
                            profiles[
                                line.employee_payroll_profile_version_id
                            ].effective_to.isoformat()
                            if profiles[line.employee_payroll_profile_version_id].effective_to
                            else None
                        ),
                        "expense_role": profiles[
                            line.employee_payroll_profile_version_id
                        ].expense_role,
                        "social_insurance_base_fen": profiles[
                            line.employee_payroll_profile_version_id
                        ].social_insurance_base_fen,
                        "housing_fund_base_fen": profiles[
                            line.employee_payroll_profile_version_id
                        ].housing_fund_base_fen,
                        "social_insurance_participating": profiles[
                            line.employee_payroll_profile_version_id
                        ].social_insurance_participating,
                        "housing_fund_participating": profiles[
                            line.employee_payroll_profile_version_id
                        ].housing_fund_participating,
                    }
                    if line.employee_payroll_profile_version_id in profiles
                    else None,
                    "payroll_line": self._payroll_line_dict(line),
                }
                for line in lines
            ],
            "policy": {
                "policy_version_id": str(policy.id) if policy else None,
                "version": policy.version if policy else batch.policy_snapshot.get("version"),
                "effective_from": policy.effective_from.isoformat() if policy else None,
                "effective_to": policy.effective_to.isoformat()
                if policy and policy.effective_to
                else None,
                "official_sources": sources,
            },
            "confirmation": {
                "status": batch.status,
                "confirmed_by": batch.confirmed_by,
                "confirmation_note": batch.confirmation_note,
                "confirmed_at": batch.confirmed_at.isoformat() if batch.confirmed_at else None,
                "business_event_id": str(batch.business_event_id)
                if batch.business_event_id
                else None,
            },
            "evidence": [
                {
                    "id": str(item.id),
                    "sha256": item.sha256,
                    "original_name": item.original_name,
                    "source": item.source,
                    "media_type": item.media_type,
                    "size_bytes": item.size_bytes,
                }
                for item in evidence
            ],
            "business_events": [
                {
                    "id": str(event.id),
                    "event_type": event.event_type,
                    "status": event.status,
                    "business_date": event.business_date.isoformat(),
                    "payment_date": event.payment_date.isoformat() if event.payment_date else None,
                    "posting_date": event.posting_date.isoformat(),
                    "rule_version": event.rule_version,
                    "reversal_of_event_id": (
                        str(reversal_parent_by_id[event.id])
                        if event.id in reversal_parent_by_id
                        else None
                    ),
                    "trace": event.rule_trace,
                    "reversed_by_event_id": (
                        str(event.reversed_by_event_id) if event.reversed_by_event_id else None
                    ),
                }
                for event in events
            ],
            "event_evidence": [
                {
                    "event_id": str(row.event_id),
                    "evidence_id": str(row.evidence_id),
                    "relation_kind": row.relation_kind,
                }
                for row in event_evidence_rows
            ],
            "vouchers": [
                {
                    "id": str(voucher.id),
                    "event_id": str(voucher.event_id),
                    "voucher_number": voucher.voucher_number,
                    "status": voucher.status,
                    "posting_date": voucher.posting_date.isoformat(),
                    "reversal_of_voucher_id": (
                        str(voucher.reversal_of_voucher_id)
                        if voucher.reversal_of_voucher_id
                        else None
                    ),
                    "lines": [
                        {
                            "line_number": line.line_number,
                            "account_code": line.account.code,
                            "debit_fen": line.debit_fen,
                            "credit_fen": line.credit_fen,
                            "counterparty_id": (
                                str(line.counterparty_id) if line.counterparty_id else None
                            ),
                        }
                        for line in voucher.lines
                    ],
                }
                for voucher in vouchers
            ],
            "open_items": [
                {
                    "id": str(item.id),
                    "source_event_id": str(item.source_event_id),
                    "counterparty_id": str(item.counterparty_id),
                    "item_type": item.item_type,
                    "payable_category": item.payable_category,
                    "payable_agency_code": item.payable_agency_code,
                    "insurance_kind": item.insurance_kind,
                    "original_amount_fen": item.original_amount_fen,
                    "settled_amount_fen": item.settled_amount_fen,
                    "status": item.status,
                }
                for item in sorted(open_items_by_id.values(), key=lambda item: item.id)
            ],
            "settlements": [
                {
                    "id": str(settlement.id),
                    "open_item_id": str(settlement.open_item_id),
                    "payment_event_id": str(settlement.payment_event_id),
                    "amount_fen": settlement.amount_fen,
                    "reversed": settlement.reversed,
                }
                for settlement in sorted(settlements_by_id.values(), key=lambda item: item.id)
            ],
            "payroll_event_links": [
                {
                    "id": str(link.id),
                    "event_id": str(link.event_id),
                    "link_kind": link.link_kind,
                    "payroll_batch_id": str(link.payroll_batch_id),
                    "payroll_batch": {
                        "batch_kind": linked_batches[link.payroll_batch_id].batch_kind,
                        "payroll_period": linked_batches[link.payroll_batch_id].payroll_period,
                        "policy_version_id": str(
                            linked_batches[link.payroll_batch_id].policy_version_id
                        ),
                        "reversal_of_batch_id": (
                            str(linked_batches[link.payroll_batch_id].reversal_of_batch_id)
                            if linked_batches[link.payroll_batch_id].reversal_of_batch_id
                            else None
                        ),
                    }
                    if link.payroll_batch_id in linked_batches
                    else None,
                    "source_payment_event_id": (
                        str(link.source_payment_event_id) if link.source_payment_event_id else None
                    ),
                    "source_payment_event": (
                        {
                            "event_type": link_source_events[
                                link.source_payment_event_id
                            ].event_type,
                            "status": link_source_events[link.source_payment_event_id].status,
                            "reversed_by_event_id": (
                                str(
                                    link_source_events[
                                        link.source_payment_event_id
                                    ].reversed_by_event_id
                                )
                                if link_source_events[
                                    link.source_payment_event_id
                                ].reversed_by_event_id
                                else None
                            ),
                        }
                        if link.source_payment_event_id in link_source_events
                        else None
                    ),
                    "source_open_item_id": (
                        str(link.source_open_item_id) if link.source_open_item_id else None
                    ),
                    "source_open_item": (
                        {
                            "payable_category": link_source_items[
                                link.source_open_item_id
                            ].payable_category,
                            "payable_agency_code": link_source_items[
                                link.source_open_item_id
                            ].payable_agency_code,
                            "insurance_kind": link_source_items[
                                link.source_open_item_id
                            ].insurance_kind,
                            "source_event_id": str(
                                link_source_items[link.source_open_item_id].source_event_id
                            ),
                        }
                        if link.source_open_item_id in link_source_items
                        else None
                    ),
                }
                for link in payroll_event_links
            ],
            "payments": [
                {
                    "event_id": str(event.id),
                    "event_type": event.event_type,
                    "bank_transactions": [
                        {
                            "id": str(row.id),
                            "fingerprint": row.fingerprint,
                            "booking_date": row.booking_date.isoformat(),
                            "amount_fen": row.amount_fen,
                        }
                        for row in matched_bank
                        if row.matched_event_id == event.id
                    ],
                    "bank_match_history": [
                        {
                            "match_id": str(match.id),
                            "bank_transaction_id": str(match.bank_transaction_id),
                            "fingerprint": bank_by_id[match.bank_transaction_id].fingerprint,
                            "current": match.invalidated_by_event_id is None,
                            "invalidated_by_event_id": (
                                str(match.invalidated_by_event_id)
                                if match.invalidated_by_event_id
                                else None
                            ),
                            "invalidated_at": (
                                match.invalidated_at.isoformat() if match.invalidated_at else None
                            ),
                        }
                        for match in bank_matches
                        if match.event_id == event.id
                    ],
                }
                for event in payment_events
            ],
            "reversal_chain": [
                {
                    "batch_id": str(item.id),
                    "status": item.status,
                    "reversal_of_batch_id": (
                        str(item.reversal_of_batch_id) if item.reversal_of_batch_id else None
                    ),
                    "business_event_id": str(item.business_event_id)
                    if item.business_event_id
                    else None,
                }
                for item in reversal_batches
            ],
            "canonical_reversal_chain": [
                {
                    "event_id": str(event.id),
                    "reversal_of_event_id": (
                        str(reversal_parent_by_id[event.id])
                        if event.id in reversal_parent_by_id
                        else None
                    ),
                    "reversed_by_event_id": (
                        str(event.reversed_by_event_id) if event.reversed_by_event_id else None
                    ),
                    "payroll_event_link_ids": [
                        str(link.id) for link in payroll_links_by_event_id.get(event.id, [])
                    ],
                }
                for event in events
            ],
            "audit_log": [
                {
                    "id": str(log.id),
                    "event_id": str(log.event_id) if log.event_id else None,
                    "action": log.action,
                    "actor": log.actor,
                    "details": log.details,
                    "created_at": log.created_at.isoformat(),
                }
                for log in related_audit_logs
            ],
        }

    @staticmethod
    def _payroll_policy_sources(
        policy: PayrollPolicyVersion | None, snapshot: dict[str, Any]
    ) -> list[dict[str, str]]:
        sources: list[dict[str, str]] = []
        if policy is not None:
            sources.append({"kind": "payroll_policy", "url": policy.source_url})
        elif snapshot.get("source_url"):
            sources.append({"kind": "payroll_policy", "url": str(snapshot["source_url"])})
        parameters = snapshot.get("parameters", {})
        if isinstance(parameters, dict):
            for kind in ("income_tax", "annual_bonus"):
                rule = parameters.get(kind)
                if isinstance(rule, dict) and rule.get("primary_source_url"):
                    sources.append({"kind": kind, "url": str(rule["primary_source_url"])})
        return sources

    def _reserve_payroll_tax_state_slots(
        self, batch: PayrollBatch, lines: list[PayrollLine]
    ) -> None:
        """Atomically reserve/advance the only tax-state slot for each employee-month."""

        tax_state_lines = [
            line for line in lines if self._line_uses_cumulative_tax_state(batch, line)
        ]
        if not tax_state_lines:
            return
        tax_period = self._batch_tax_period(batch)
        year, month = tax_period.year, tax_period.month
        employee_ids = [line.employee_id for line in tax_state_lines]
        later = self.session.scalars(
            select(PayrollTaxStateSlot)
            .where(
                PayrollTaxStateSlot.org_id == batch.org_id,
                PayrollTaxStateSlot.employee_id.in_(employee_ids),
                PayrollTaxStateSlot.tax_year == year,
                PayrollTaxStateSlot.tax_month > month,
            )
            .with_for_update()
        ).all()
        if later:
            raise CalculationValidationError(
                "LATER_PAYROLL_TAX_STATE_EXISTS", "later tax state must be reversed first"
            )
        if batch.batch_kind == PayrollBatchKind.REGULAR.value:
            insert_stmt = (
                pg_insert(PayrollTaxStateSlot)
                if self.session.bind and self.session.bind.dialect.name == "postgresql"
                else sqlite_insert(PayrollTaxStateSlot)
            )
            for line in tax_state_lines:
                inserted_slot_id = self.session.scalar(
                    insert_stmt.values(
                        org_id=batch.org_id,
                        employee_id=line.employee_id,
                        tax_year=year,
                        tax_month=month,
                        regular_batch_id=batch.id,
                        final_batch_id=batch.id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["org_id", "employee_id", "tax_year", "tax_month"]
                    )
                    .returning(PayrollTaxStateSlot.id)
                )
                if inserted_slot_id is None:
                    raise CalculationValidationError(
                        "PAYROLL_TAX_STATE_SLOT_ALREADY_EXISTS", "regular tax slot already exists"
                    )
            return
        for line in tax_state_lines:
            if line.regular_payroll_batch_id is None:
                raise CalculationValidationError(
                    "INVALID_REGULAR_PAYROLL_DEPENDENCY", "combined bonus needs a regular batch"
                )
            result = self.session.execute(
                update(PayrollTaxStateSlot)
                .where(
                    PayrollTaxStateSlot.org_id == batch.org_id,
                    PayrollTaxStateSlot.employee_id == line.employee_id,
                    PayrollTaxStateSlot.tax_year == year,
                    PayrollTaxStateSlot.tax_month == month,
                    PayrollTaxStateSlot.regular_batch_id == line.regular_payroll_batch_id,
                    PayrollTaxStateSlot.final_batch_id == line.regular_payroll_batch_id,
                )
                .values(final_batch_id=batch.id)
            )
            if result.rowcount != 1:
                raise CalculationValidationError(
                    "PAYROLL_TAX_STATE_SLOT_NOT_REGULAR_FINAL", "combined bonus slot is unavailable"
                )

    def _calculate_payroll(self, request: PreviewPayrollRequest) -> dict[str, Any]:
        period = YearMonth(int(request.payroll_period[:4]), int(request.payroll_period[5:]))
        tax_period = (
            period
            if request.batch_kind == PayrollBatchKind.REGULAR
            else YearMonth(request.payment_date.year, request.payment_date.month)
        )
        tax_policy_date = (
            period.end_date
            if request.batch_kind == PayrollBatchKind.REGULAR
            else request.payment_date
        )
        contribution_policy_record = self._effective_payroll_policy(request.org_id, period.end_date)
        tax_policy_record = self._effective_payroll_policy(request.org_id, tax_policy_date)
        if contribution_policy_record is None or tax_policy_record is None:
            required_policy_dates = []
            if contribution_policy_record is None:
                required_policy_dates.append("contribution_policy_version")
            if tax_policy_record is None:
                required_policy_dates.append("income_tax_policy_version")
            return {
                "missing": [
                    {
                        "code": "payroll_policy",
                        "message": (
                            "an effective contribution policy at the payroll-period end and "
                            "income-tax policy for the tax period are required"
                        ),
                        "fields": required_policy_dates,
                    }
                ],
                "scenarios": [],
            }
        contribution_policy, _, _ = self._calculator_policies(contribution_policy_record)
        _, tax_policy, bonus_policy = self._calculator_policies(
            tax_policy_record,
            require_bonus_policy=(
                request.batch_kind == PayrollBatchKind.ANNUAL_BONUS
                and request.tax_method == AnnualBonusTaxMethod.SEPARATE
            ),
        )
        if (
            request.batch_kind == PayrollBatchKind.ANNUAL_BONUS
            and request.tax_method == AnnualBonusTaxMethod.COMBINED
            and bonus_policy is not None
        ):
            try:
                bonus_policy.assert_effective(request.payment_date)
            except ExpiredPolicyError:
                # A combined bonus is governed by the current cumulative wage-tax
                # rule; an expired separate-method policy must not block it.
                bonus_policy = None
        # Regular wage contributions and cumulative tax use the payroll period;
        # a later bank settlement date must not move a reported wage into a new
        # declaration month. Annual-bonus tax keeps its actual payment period.
        # Do not start from one
        # outer policy JSON document and patch a few display fields: doing that
        # makes the hash evidence claim a different rule from the one used by
        # the calculator when a payment crosses a policy year.
        contribution_parameters = deepcopy(contribution_policy_record.parameters)
        tax_parameters = deepcopy(tax_policy_record.parameters)
        merged_payment_targets = {
            **self._payment_targets(contribution_parameters),
            "individual_income_tax": self._payment_targets(tax_parameters).get(
                "individual_income_tax", {}
            ),
        }
        snapshot_parameters = {
            "contribution_rules": deepcopy(contribution_parameters.get("contribution_rules", [])),
            "employee_contribution_shortfall_treatment": contribution_parameters.get(
                "employee_contribution_shortfall_treatment", "reject"
            ),
            "income_tax": deepcopy(tax_parameters["income_tax"]),
            "annual_bonus": deepcopy(tax_parameters.get("annual_bonus")),
            "payment_targets": merged_payment_targets,
        }
        policy_snapshot = {
            "id": str(tax_policy_record.id),
            "version": tax_policy_record.version,
            "region": tax_policy_record.region,
            "effective_from": tax_policy_record.effective_from.isoformat(),
            "effective_to": (
                tax_policy_record.effective_to.isoformat()
                if tax_policy_record.effective_to
                else None
            ),
            "source_url": tax_policy_record.source_url,
            "parameters": snapshot_parameters,
            "contribution_policy": self._policy_snapshot(contribution_policy_record),
            "income_tax_policy": {
                **self._policy_snapshot(tax_policy_record),
                "rule_id": "income_tax",
                "version": tax_policy.version,
                "effective_from": tax_policy.effective_from.isoformat(),
                "effective_to": (
                    tax_policy.effective_to.isoformat() if tax_policy.effective_to else None
                ),
                "primary_source_url": tax_policy.primary_source_url,
                "legal_basis_source_url": tax_policy.legal_basis_source_url,
            },
            "annual_bonus_policy": (
                {
                    **self._policy_snapshot(tax_policy_record),
                    "rule_id": "annual_bonus",
                    "version": bonus_policy.version,
                    "effective_from": bonus_policy.effective_from.isoformat(),
                    "effective_to": (
                        bonus_policy.effective_to.isoformat() if bonus_policy.effective_to else None
                    ),
                    "primary_source_url": bonus_policy.primary_source_url,
                }
                if bonus_policy is not None
                else None
            ),
        }
        missing: list[dict[str, Any]] = []
        scenarios: list[dict[str, Any]] = []
        prepared_lines: list[dict[str, Any]] = []
        input_snapshots: list[dict[str, Any]] = []
        actual_item_ids: set[uuid.UUID] = set()
        first_wage_treatment_ids: set[uuid.UUID] = set()
        for item in request.employee_items:
            employee = self._employee_for_org(request.org_id, item.employee_id)
            if employee is None:
                missing.append(
                    {
                        "code": "employee",
                        "message": f"employee {item.employee_id} was not found",
                        "fields": ["employee_id"],
                    }
                )
                continue
            if not self._employee_active_for_period(employee, period):
                raise CalculationValidationError(
                    "EMPLOYEE_NOT_ACTIVE",
                    f"employee {employee.employee_code} is not active for {period}",
                )
            profile = self._effective_profile(employee.id, period.end_date)
            if profile is None:
                missing.append(
                    {
                        "code": "employee_payroll_profile",
                        "message": (
                            f"employee {employee.employee_code} has no effective payroll profile"
                        ),
                        "fields": ["employee_payroll_profile_version"],
                    }
                )
                continue
            uses_wage_tax = (
                request.batch_kind == PayrollBatchKind.ANNUAL_BONUS
                or item.wage_tax_declaration_state
                == PayrollWageTaxDeclarationState.DECLARED
            )
            if uses_wage_tax and profile.resident_employee is None:
                missing.append(
                    {
                        "code": "resident_employee",
                        "message": (
                            f"employee {employee.employee_code} needs an explicit resident "
                            "individual status before wage-tax calculation"
                        ),
                        "fields": ["resident_employee"],
                    }
                )
                continue
            if uses_wage_tax and not profile.resident_employee:
                raise CalculationValidationError(
                    "UNSUPPORTED_EMPLOYEE_TYPE", "phase 1 supports resident employees only"
                )
            if request.batch_kind == PayrollBatchKind.REGULAR:
                required_regular_fields = (
                    "tax_reported_salary_fen",
                    "special_additional_deduction_fen",
                    "other_legal_deduction_fen",
                )
                absent_fields = [
                    field_name
                    for field_name in required_regular_fields
                    if field_name not in item.model_fields_set
                ]
                if absent_fields:
                    missing.append(
                        {
                            "code": "regular_payroll_items",
                            "message": (
                                f"employee {employee.employee_code} needs explicit regular "
                                "tax-reported salary and deduction facts"
                            ),
                            "fields": absent_fields,
                        }
                    )
                    continue
                if (
                    item.wage_tax_declaration_state
                    == PayrollWageTaxDeclarationState.DECLARED
                    and employee.tax_withholding_start_date is None
                ):
                    missing.append(
                        {
                            "code": "tax_withholding_start_date",
                            "message": (
                                f"employee {employee.employee_code} needs an explicit tax "
                                "withholding start date"
                            ),
                            "fields": ["tax_withholding_start_date"],
                        }
                    )
                    continue
            profile_snapshot = {
                "id": str(profile.id),
                "effective_from": profile.effective_from.isoformat(),
                "effective_to": profile.effective_to.isoformat() if profile.effective_to else None,
                "expense_role": profile.expense_role,
                "social_insurance_base_fen": profile.social_insurance_base_fen,
                "housing_fund_base_fen": profile.housing_fund_base_fen,
                "social_insurance_participating": profile.social_insurance_participating,
                "housing_fund_participating": profile.housing_fund_participating,
                "resident_employee": profile.resident_employee,
            }
            if request.batch_kind == PayrollBatchKind.REGULAR:
                policy_contribution = calculate_contributions(
                    contribution_policy,
                    ContributionBases(
                        profile.social_insurance_base_fen,
                        profile.housing_fund_base_fen,
                        profile.social_insurance_participating,
                        profile.housing_fund_participating,
                    ),
                    period.end_date,
                )
                active_actuals = self._active_contribution_actual_items(
                    request.org_id, employee.id, request.payroll_period
                )
                contribution = apply_contribution_actuals(
                    policy_contribution,
                    tuple(
                        ContributionActualOverride(
                            actual_item_id=str(actual.id),
                            code=actual.insurance_kind,
                            base_kind=ContributionBaseKind(actual.contribution_group),
                            actual_state=actual.actual_state,
                            employee_amount_fen=actual.employee_amount_fen,
                            employer_amount_fen=actual.employer_amount_fen,
                        )
                        for actual in active_actuals
                    ),
                )
                actual_item_ids.update(actual.id for actual in active_actuals)
                payroll_input = RegularPayrollInput(
                    tax_reported_salary_fen=item.tax_reported_salary_fen or 0,
                    special_additional_deduction_fen=item.special_additional_deduction_fen,
                    other_legal_deduction_fen=item.other_legal_deduction_fen,
                    accounting_gross_salary_fen=item.accounting_gross_salary_fen,
                )
                shortfall_treatment = EmployeeContributionShortfallTreatment(
                    contribution_parameters.get(
                        "employee_contribution_shortfall_treatment", "reject"
                    )
                )
                contribution_burden = allocate_contribution_burden(
                    contribution,
                    payroll_input.gross_salary_fen,
                    shortfall_treatment,
                )
                if (
                    item.wage_tax_declaration_state
                    == PayrollWageTaxDeclarationState.NOT_DECLARED
                ):
                    prepared_lines.append(
                        self._unreported_regular_prepared_line(
                            employee,
                            profile,
                            item,
                            contribution,
                            contribution_burden,
                        )
                    )
                    input_snapshots.append(
                        {
                            "employee_id": str(employee.id),
                            "profile": profile_snapshot,
                            "wage_tax_declaration_state": "not_declared",
                            "tax_withholding_start_date": None,
                            "prior_tax_state": None,
                            "contribution_actual_item_ids": [
                                str(actual.id) for actual in active_actuals
                            ],
                            "first_wage_tax_treatment": None,
                        }
                    )
                    continue

                later_slot = self.session.scalar(
                    select(PayrollTaxStateSlot.id).where(
                        PayrollTaxStateSlot.org_id == request.org_id,
                        PayrollTaxStateSlot.employee_id == employee.id,
                        PayrollTaxStateSlot.tax_year == tax_period.year,
                        PayrollTaxStateSlot.tax_month > tax_period.month,
                    )
                )
                if later_slot is not None:
                    raise CalculationValidationError(
                        "LATER_PAYROLL_TAX_STATE_EXISTS",
                        "later tax state must be reversed before back-filling payroll",
                    )
                if self._posted_regular_tax_state_for_month(
                    request.org_id, employee.id, tax_period
                ):
                    raise CalculationValidationError(
                        "DUPLICATE_REGULAR_TAX_STATE",
                        "a posted regular payroll already owns this employee tax month",
                    )
                prior_state = self._prior_tax_state(employee, tax_period)
                if prior_state is None:
                    missing.append(
                        {
                            "code": "cumulative_tax_state",
                            "message": (
                                f"employee {employee.employee_code} needs a known zero state "
                                "or opening state"
                            ),
                            "fields": ["payroll_opening_state"],
                        }
                    )
                    continue
                first_wage_treatment = self._active_first_wage_tax_treatment(
                    request.org_id, employee.id, tax_period.year
                )
                standard_deduction_start_month = None
                if (
                    first_wage_treatment is not None
                    and tax_period.month >= first_wage_treatment.first_wage_month
                ):
                    first_wage_treatment_ids.add(first_wage_treatment.id)
                    if first_wage_treatment.treatment_state == "eligible":
                        standard_deduction_start_month = 1
                tax_input = CumulativeTaxPeriodInput(
                    income_date=period.end_date,
                    withholding_start_date=employee.tax_withholding_start_date,
                    income_fen=payroll_input.taxable_income_fen,
                    tax_exempt_income_fen=0,
                    employee_contributions_fen=contribution_burden.employee_total_fen,
                    special_additional_deduction_fen=payroll_input.special_additional_deduction_fen,
                    other_legal_deduction_fen=payroll_input.other_legal_deduction_fen,
                    tax_relief_fen=item.tax_relief_fen,
                    standard_deduction_start_month=standard_deduction_start_month,
                )
                tax = calculate_cumulative_withholding(
                    tax_policy, tax_period, prior_state, tax_input
                )
                result = calculate_regular_payroll(
                    payroll_input, contribution, tax, contribution_burden
                )
                prepared_lines.append(
                    self._regular_prepared_line(employee, profile, item, result, tax.new_state)
                )
                input_snapshots.append(
                    {
                        "employee_id": str(employee.id),
                        "profile": profile_snapshot,
                        "wage_tax_declaration_state": "declared",
                        "accounting_gross_salary_fen": payroll_input.gross_salary_fen,
                        "tax_reported_salary_fen": payroll_input.tax_reported_salary_fen,
                        "tax_reporting_difference_reason": (
                            item.tax_reporting_difference_reason
                        ),
                        "tax_withholding_start_date": (
                            employee.tax_withholding_start_date.isoformat()
                        ),
                        "prior_tax_state": self._tax_state_dict(prior_state),
                        "contribution_actual_item_ids": [
                            str(actual.id) for actual in active_actuals
                        ],
                        "first_wage_tax_treatment": (
                            {
                                "id": str(first_wage_treatment.id),
                                "tax_year": first_wage_treatment.tax_year,
                                "first_wage_month": first_wage_treatment.first_wage_month,
                                "treatment_state": first_wage_treatment.treatment_state,
                                "legal_basis_url": first_wage_treatment.legal_basis_url,
                                "standard_deduction_start_month": (
                                    standard_deduction_start_month
                                ),
                            }
                            if first_wage_treatment is not None
                            else None
                        ),
                    }
                )
            else:
                if bonus_policy is None and request.tax_method != AnnualBonusTaxMethod.COMBINED:
                    raise CalculationValidationError(
                        "INVALID_POLICY_PARAMETERS", "annual_bonus policy parameters are required"
                    )
                if item.regular_payroll_batch_id is None:
                    if request.tax_method == AnnualBonusTaxMethod.SEPARATE:
                        used = self.session.scalar(
                            select(AnnualBonusUsage).where(
                                AnnualBonusUsage.employee_id == employee.id,
                                AnnualBonusUsage.tax_year == tax_period.year,
                            )
                        )
                        scenario_result = calculate_annual_bonus_scenarios(
                            bonus_policy,
                            tax_policy,
                            AnnualBonusScenarioInput(
                                period=tax_period,
                                payment_date=request.payment_date,
                                bonus_fen=item.annual_bonus_fen,
                                prior_tax_state=None,
                                regular_period_input=None,
                                usage=CalculatorAnnualBonusUsage(tax_period.year, used is not None),
                            ),
                        )
                        selected = select_annual_bonus_tax_method(
                            scenario_result, AnnualBonusTaxMethod.SEPARATE
                        )
                        scenarios.append(
                            {
                                "employee_id": str(employee.id),
                                "separate": self._bonus_scenario_dict(scenario_result.separate),
                                "combined": self._bonus_scenario_dict(scenario_result.combined),
                            }
                        )
                        prepared_lines.append(
                            self._bonus_prepared_line(
                                employee,
                                profile,
                                item,
                                selected,
                                None,
                                regular_payroll_batch_id=None,
                            )
                        )
                        input_snapshots.append(
                            {"employee_id": str(employee.id), "profile": profile_snapshot}
                        )
                        continue
                    missing.append(
                        {
                            "code": "annual_bonus_regular_payroll_batch",
                            "message": (
                                f"employee {employee.employee_code} needs a posted same-tax-month "
                                "regular payroll batch before annual-bonus calculation"
                            ),
                            "fields": ["regular_payroll_batch_id"],
                        }
                    )
                    scenarios.append(
                        {
                            "employee_id": str(employee.id),
                            "non_confirmable": True,
                            "reason": "POSTED_REGULAR_PAYROLL_BATCH_REQUIRED",
                        }
                    )
                    continue
                regular_batch, regular_line, prior_state = self._regular_payroll_dependency(
                    request.org_id,
                    employee,
                    item.regular_payroll_batch_id,
                    tax_period,
                )
                regular_tax_input = self._regular_tax_input_from_posted_line(
                    employee, regular_batch, regular_line
                )
                used = self.session.scalar(
                    select(AnnualBonusUsage).where(
                        AnnualBonusUsage.employee_id == employee.id,
                        AnnualBonusUsage.tax_year == tax_period.year,
                    )
                )
                if bonus_policy is None:
                    combined_tax = calculate_cumulative_withholding(
                        tax_policy,
                        tax_period,
                        prior_state,
                        CumulativeTaxPeriodInput(
                            income_date=request.payment_date,
                            withholding_start_date=self._required_tax_withholding_start_date(
                                employee
                            ),
                            income_fen=regular_tax_input.income_fen + item.annual_bonus_fen,
                            tax_exempt_income_fen=regular_tax_input.tax_exempt_income_fen,
                            employee_contributions_fen=regular_tax_input.employee_contributions_fen,
                            special_additional_deduction_fen=(
                                regular_tax_input.special_additional_deduction_fen
                            ),
                            other_legal_deduction_fen=regular_tax_input.other_legal_deduction_fen,
                            tax_relief_fen=regular_tax_input.tax_relief_fen,
                            standard_deduction_start_month=(
                                regular_tax_input.standard_deduction_start_month
                            ),
                        ),
                    )
                    selected = AnnualBonusTaxScenario(
                        method=AnnualBonusTaxMethod.COMBINED,
                        tax_fen=max(
                            0,
                            combined_tax.current_withholding_tax_fen
                            - regular_line.individual_income_tax_fen,
                        ),
                        net_bonus_fen=(
                            item.annual_bonus_fen
                            - max(
                                0,
                                combined_tax.current_withholding_tax_fen
                                - regular_line.individual_income_tax_fen,
                            )
                        ),
                        available=True,
                        unavailable_reason=None,
                    )
                    state_after = combined_tax.new_state
                    scenarios.append(
                        {
                            "employee_id": str(employee.id),
                            "separate": {
                                "method": AnnualBonusTaxMethod.SEPARATE.value,
                                "tax_fen": None,
                                "net_bonus_fen": None,
                                "available": False,
                                "unavailable_reason": "ANNUAL_BONUS_POLICY_NOT_EFFECTIVE",
                            },
                            "combined": self._bonus_scenario_dict(selected),
                        }
                    )
                else:
                    scenario_result = calculate_annual_bonus_scenarios(
                        bonus_policy,
                        tax_policy,
                        AnnualBonusScenarioInput(
                            period=tax_period,
                            payment_date=request.payment_date,
                            bonus_fen=item.annual_bonus_fen,
                            prior_tax_state=prior_state,
                            regular_period_input=regular_tax_input,
                            usage=CalculatorAnnualBonusUsage(tax_period.year, used is not None),
                            regular_current_withholding_fen=regular_line.individual_income_tax_fen,
                        ),
                    )
                    scenarios.append(
                        {
                            "employee_id": str(employee.id),
                            "separate": self._bonus_scenario_dict(scenario_result.separate),
                            "combined": self._bonus_scenario_dict(scenario_result.combined),
                        }
                    )
                    if request.tax_method is None:
                        missing.append(
                            {
                                "code": "annual_bonus_tax_method",
                                "message": (
                                    "an explicit annual bonus tax method selection is required"
                                ),
                                "fields": ["tax_method"],
                            }
                        )
                        continue
                    selected = select_annual_bonus_tax_method(scenario_result, request.tax_method)
                    state_after = None
                    if request.tax_method == AnnualBonusTaxMethod.COMBINED:
                        state_after = calculate_cumulative_withholding(
                            tax_policy,
                            tax_period,
                            prior_state,
                            CumulativeTaxPeriodInput(
                                income_date=request.payment_date,
                                withholding_start_date=self._required_tax_withholding_start_date(
                                    employee
                                ),
                                income_fen=regular_tax_input.income_fen + item.annual_bonus_fen,
                                tax_exempt_income_fen=regular_tax_input.tax_exempt_income_fen,
                                employee_contributions_fen=regular_tax_input.employee_contributions_fen,
                                special_additional_deduction_fen=(
                                    regular_tax_input.special_additional_deduction_fen
                                ),
                                other_legal_deduction_fen=(
                                    regular_tax_input.other_legal_deduction_fen
                                ),
                                tax_relief_fen=regular_tax_input.tax_relief_fen,
                                standard_deduction_start_month=(
                                    regular_tax_input.standard_deduction_start_month
                                ),
                            ),
                        ).new_state
                prepared_lines.append(
                    self._bonus_prepared_line(
                        employee,
                        profile,
                        item,
                        selected,
                        state_after,
                        regular_payroll_batch_id=regular_batch.id,
                    )
                )
                input_snapshots.append(
                    {
                        "employee_id": str(employee.id),
                        "profile": profile_snapshot,
                        "prior_tax_state": self._tax_state_dict(prior_state),
                        "regular_payroll_batch_id": str(regular_batch.id),
                        "regular_payroll_line_id": str(regular_line.id),
                        "regular_tax_input": self._tax_input_dict(regular_tax_input),
                    }
                )
        if missing:
            return {"missing": missing, "scenarios": scenarios}
        payment_targets = merged_payment_targets
        absent_payment_targets = [
            key
            for key in ("social_insurance", "housing_fund", "individual_income_tax")
            if key not in payment_targets
        ]
        if absent_payment_targets:
            return {
                "missing": [
                    {
                        "code": "statutory_payment_targets",
                        "message": "payroll policy is missing statutory payment targets",
                        "fields": [f"payment_targets.{key}" for key in absent_payment_targets],
                    }
                ],
                "scenarios": scenarios,
            }
        calculation_input = {
            "request": request.model_dump(mode="json"),
            "employee_snapshots": input_snapshots,
        }
        hash_payload = {
            "calculation_input": calculation_input,
            "policy_snapshot": policy_snapshot,
            "lines": prepared_lines,
            "actual_item_ids": sorted(actual_item_ids, key=str),
            "first_wage_treatment_ids": sorted(first_wage_treatment_ids, key=str),
        }
        calculation_hash = hashlib.sha256(
            json.dumps(
                hash_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        summary = {
            "gross_salary_fen": sum(line["gross_salary_fen"] for line in prepared_lines),
            "net_salary_fen": sum(line["net_salary_fen"] for line in prepared_lines),
            "employer_social_insurance_fen": sum(
                line["employer_social_insurance_fen"] for line in prepared_lines
            ),
            "employer_housing_fund_fen": sum(
                line["employer_housing_fund_fen"] for line in prepared_lines
            ),
            "individual_income_tax_fen": sum(
                line["individual_income_tax_fen"] for line in prepared_lines
            ),
        }
        trace = [
            {
                "stage": "payroll_calculated",
                "batch_kind": request.batch_kind.value,
                "payroll_period": request.payroll_period,
                "payment_date": request.payment_date.isoformat(),
                "policy_version": tax_policy_record.version,
                "policy_sources": {
                    "contribution": policy_snapshot["contribution_policy"],
                    "income_tax": policy_snapshot["income_tax_policy"],
                    "annual_bonus": policy_snapshot["annual_bonus_policy"],
                },
                "calculation_hash": calculation_hash,
                "summary": summary,
            }
        ]
        return {
            "missing": [],
            "scenarios": scenarios,
            "policy": tax_policy_record,
            "policy_snapshot": policy_snapshot,
            "calculation_input": calculation_input,
            "calculation_hash": calculation_hash,
            "lines": prepared_lines,
            "actual_item_ids": sorted(actual_item_ids, key=str),
            "first_wage_treatment_ids": sorted(first_wage_treatment_ids, key=str),
            "summary": summary,
            "trace": trace,
        }

    def _calculator_policies(
        self, policy: PayrollPolicyVersion, *, require_bonus_policy: bool = False
    ) -> tuple[ContributionPolicy, CumulativeIncomeTaxPolicy, AnnualBonusTaxPolicy | None]:
        params = policy.parameters
        try:
            contribution_rules = tuple(
                ContributionRule(
                    code=str(item["code"]),
                    base_kind=str(item["base_kind"]),
                    employee_rate=self._decimal_parameter(item["employee_rate"]),
                    employer_rate=self._decimal_parameter(item["employer_rate"]),
                    minimum_base_fen=item["minimum_base_fen"],
                    maximum_base_fen=item["maximum_base_fen"],
                    rounding_rule=RoundingRule(item["rounding_rule"]),
                    enabled=item.get("enabled", True),
                )
                for item in params["contribution_rules"]
            )
            contribution_policy = ContributionPolicy(
                version=policy.version,
                jurisdiction=policy.region,
                effective_from=policy.effective_from,
                effective_to=policy.effective_to,
                primary_source_url=policy.source_url,
                rules=contribution_rules,
            )
            tax_data = params["income_tax"]
            tax_policy = CumulativeIncomeTaxPolicy(
                version=tax_data["version"],
                effective_from=date.fromisoformat(tax_data["effective_from"]),
                effective_to=(
                    date.fromisoformat(tax_data["effective_to"])
                    if tax_data.get("effective_to")
                    else None
                ),
                primary_source_url=tax_data["primary_source_url"],
                legal_basis_source_url=tax_data["legal_basis_source_url"],
                monthly_standard_deduction_fen=tax_data["monthly_standard_deduction_fen"],
                brackets=tuple(
                    TaxBracket(
                        upper_bound_fen=item.get("upper_bound_fen"),
                        rate=self._decimal_parameter(item["rate"]),
                        quick_deduction_fen=item["quick_deduction_fen"],
                    )
                    for item in tax_data["brackets"]
                ),
            )
            bonus_data = params.get("annual_bonus")
            if require_bonus_policy and bonus_data is None:
                raise CalculationValidationError(
                    "INVALID_POLICY_PARAMETERS", "annual_bonus policy parameters are required"
                )
            bonus_policy = None
            if bonus_data is not None:
                bonus_policy = AnnualBonusTaxPolicy(
                    version=bonus_data["version"],
                    effective_from=date.fromisoformat(bonus_data["effective_from"]),
                    effective_to=(
                        date.fromisoformat(bonus_data["effective_to"])
                        if bonus_data.get("effective_to")
                        else None
                    ),
                    primary_source_url=bonus_data["primary_source_url"],
                    brackets=tuple(
                        AnnualBonusBracket(
                            upper_monthly_average_fen=item.get("upper_monthly_average_fen"),
                            rate=self._decimal_parameter(item["rate"]),
                            quick_deduction_fen=item["quick_deduction_fen"],
                        )
                        for item in bonus_data["brackets"]
                    ),
                )
        except CalculationValidationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise CalculationValidationError(
                "INVALID_POLICY_PARAMETERS",
                "payroll policy parameters do not match the calculator contract",
            ) from exc
        return contribution_policy, tax_policy, bonus_policy

    @staticmethod
    def _decimal_parameter(value: Any) -> Decimal:
        if isinstance(value, float):
            raise CalculationValidationError(
                "INVALID_RATE", "rates must not use binary floating point"
            )
        try:
            return Decimal(str(value))
        except Exception as exc:  # Decimal exposes several implementation-specific exceptions.
            raise CalculationValidationError(
                "INVALID_RATE", "rate must be a decimal string"
            ) from exc

    def _effective_payroll_policy(
        self, org_id: uuid.UUID, on_date: date
    ) -> PayrollPolicyVersion | None:
        successor = aliased(PayrollPolicyVersion)
        matches = self.session.scalars(
            select(PayrollPolicyVersion)
            .where(
                PayrollPolicyVersion.org_id == org_id,
                PayrollPolicyVersion.effective_from <= on_date,
                (PayrollPolicyVersion.effective_to.is_(None))
                | (PayrollPolicyVersion.effective_to >= on_date),
                ~exists(
                    select(successor.id).where(successor.supersedes_id == PayrollPolicyVersion.id)
                ),
            )
            .order_by(PayrollPolicyVersion.effective_from.desc(), PayrollPolicyVersion.id)
        ).all()
        if len(matches) > 1:
            raise CalculationValidationError(
                "AMBIGUOUS_PAYROLL_POLICY", "more than one payroll policy version is effective"
            )
        return matches[0] if matches else None

    @staticmethod
    def _effective_date_ranges_overlap(
        left_from: date,
        left_to: date | None,
        right_from: date,
        right_to: date | None,
    ) -> bool:
        """Return whether two closed/open-ended effective date ranges overlap."""

        return (left_to is None or right_from <= left_to) and (
            right_to is None or left_from <= right_to
        )

    def _successor_lineage_error(
        self,
        *,
        predecessor: Any,
        version_model: Any,
        candidates: list[Any],
        effective_from: date | None,
        effective_to: date | None,
        error_prefix: str,
    ) -> str | None:
        """Allow successor overlap only with the predecessor's complete ancestry.

        PostgreSQL enforces the same rule with a deferred recursive assertion.
        This service-side check gives SQLite and public callers the identical
        stable business outcome before a transaction reaches that boundary.
        Opening states have no date range: within their fixed tax-state
        dimension every distinct non-ancestor is necessarily an overlap.
        """

        ancestor_ids: set[uuid.UUID] = set()
        current = predecessor
        while current is not None:
            if current.id in ancestor_ids:
                return f"{error_prefix}_SUCCESSOR_CYCLE"
            ancestor_ids.add(current.id)
            supersedes_id = current.supersedes_id
            current = (
                self.session.get(version_model, supersedes_id)
                if supersedes_id is not None
                else None
            )
            if supersedes_id is not None and current is None:
                return f"{error_prefix}_SUCCESSOR_CYCLE"

        for candidate in candidates:
            if candidate.id in ancestor_ids:
                continue
            if effective_from is None or self._effective_date_ranges_overlap(
                effective_from,
                effective_to,
                candidate.effective_from,
                candidate.effective_to,
            ):
                return f"{error_prefix}_NON_ANCESTOR_OVERLAP"
        return None

    @staticmethod
    def _policy_snapshot(policy: PayrollPolicyVersion) -> dict[str, Any]:
        return {
            "id": str(policy.id),
            "version": policy.version,
            "region": policy.region,
            "effective_from": policy.effective_from.isoformat(),
            "effective_to": policy.effective_to.isoformat() if policy.effective_to else None,
            "source_url": policy.source_url,
        }

    @staticmethod
    def _blocked_payroll_version_correction(
        blocking_batch_ids: set[uuid.UUID],
    ) -> dict[str, Any]:
        """Return the one deterministic activation-barrier result for all versions.

        R5 deliberately chooses the no-pending-state strategy.  A correction
        is not written at all while immutable downstream payroll facts remain.
        Keeping the complete, sorted blocking set in the response makes the
        required remediation (canonical reversal then rebuild) explainable
        without introducing a second, silently inactive version lineage.
        """

        return {
            "status": "rejected",
            "errors": ["PAYROLL_VERSION_CORRECTION_BLOCKED_BY_FINAL_FACTS"],
            "data": {
                "correction_status": "blocked_by_final_facts",
                "blocking_batch_ids": [str(batch_id) for batch_id in sorted(blocking_batch_ids)],
                "activation_condition": "reverse_blocking_batches_then_rebuild_payroll",
            },
        }

    def _posted_payroll_rows(self, org_id: uuid.UUID) -> list[tuple[PayrollLine, PayrollBatch]]:
        """Return final payroll dependencies once for correction-closure evaluation."""

        return self.session.execute(
            select(PayrollLine, PayrollBatch)
            .join(PayrollBatch, PayrollBatch.id == PayrollLine.payroll_batch_id)
            .where(
                PayrollLine.org_id == org_id,
                PayrollBatch.status == "posted",
                PayrollBatch.reversal_of_batch_id.is_(None),
            )
            .order_by(PayrollBatch.payroll_period, PayrollBatch.id, PayrollLine.id)
        ).all()

    @staticmethod
    def _tax_downstream_closure(
        rows: list[tuple[PayrollLine, PayrollBatch]],
        direct: list[tuple[PayrollLine, PayrollBatch]],
    ) -> set[uuid.UUID]:
        """Include every same-employee, same-year payroll after an affected fact.

        Cumulative regular tax, same-month combined bonus, and later payroll
        all derive from the earlier final fact.  Separate bonus batches are
        included when directly affected, while later regular/combined batches
        remain in the closure through their formal tax-state chain.
        """

        cutoffs: dict[tuple[uuid.UUID, int], YearMonth] = {}
        for line, batch in direct:
            if not FinanceService._line_uses_cumulative_tax_state(batch, line):
                continue
            period = FinanceService._batch_tax_period(batch)
            key = (line.employee_id, period.year)
            cutoff = cutoffs.get(key)
            if cutoff is None or period < cutoff:
                cutoffs[key] = period
        blocked = {batch.id for _line, batch in direct}
        for line, batch in rows:
            if not FinanceService._line_uses_cumulative_tax_state(batch, line):
                continue
            period = FinanceService._batch_tax_period(batch)
            cutoff = cutoffs.get((line.employee_id, period.year))
            if cutoff is not None and period >= cutoff:
                blocked.add(batch.id)
        return blocked

    def _profile_correction_blocking_batches(
        self,
        org_id: uuid.UUID,
        employee_id: uuid.UUID,
        predecessor_id: uuid.UUID,
        effective_from: date,
        effective_to: date | None,
    ) -> set[uuid.UUID]:
        """Find final payroll that a profile successor would reinterpret."""

        rows = self._posted_payroll_rows(org_id)
        direct = [
            (line, batch)
            for line, batch in rows
            if line.employee_id == employee_id
            and line.employee_payroll_profile_version_id == predecessor_id
            and self._effective_date_ranges_overlap(
                effective_from,
                effective_to,
                YearMonth(int(batch.payroll_period[:4]), int(batch.payroll_period[5:])).end_date,
                YearMonth(int(batch.payroll_period[:4]), int(batch.payroll_period[5:])).end_date,
            )
        ]
        return self._tax_downstream_closure(rows, direct)

    def _policy_correction_blocking_batches(
        self,
        org_id: uuid.UUID,
        predecessor_id: uuid.UUID,
        effective_from: date,
        effective_to: date | None,
    ) -> set[uuid.UUID]:
        """Find all tax or contribution policy facts a successor would replace."""

        rows = self._posted_payroll_rows(org_id)
        direct_batch_ids: set[uuid.UUID] = set()
        for _line, batch in rows:
            contribution = batch.policy_snapshot.get("contribution_policy", {})
            contribution_id = contribution.get("id") if isinstance(contribution, dict) else None
            period_end = YearMonth(
                int(batch.payroll_period[:4]), int(batch.payroll_period[5:])
            ).end_date
            uses_tax_policy = batch.policy_version_id == predecessor_id and (
                self._effective_date_ranges_overlap(
                    effective_from,
                    effective_to,
                    self._batch_tax_period(batch).end_date,
                    self._batch_tax_period(batch).end_date,
                )
            )
            uses_contribution_policy = contribution_id == str(predecessor_id) and (
                self._effective_date_ranges_overlap(
                    effective_from,
                    effective_to,
                    period_end,
                    period_end,
                )
            )
            if uses_tax_policy or uses_contribution_policy:
                direct_batch_ids.add(batch.id)
        direct = [(line, batch) for line, batch in rows if batch.id in direct_batch_ids]
        return self._tax_downstream_closure(rows, direct)

    def _opening_correction_blocking_batches(
        self, org_id: uuid.UUID, employee_id: uuid.UUID, tax_year: int, through_month: int
    ) -> set[uuid.UUID]:
        """Opening state affects only later payroll lines that advance cumulative tax."""

        rows = self._posted_payroll_rows(org_id)
        return {
            batch.id
            for line, batch in rows
            if line.employee_id == employee_id
            and self._line_uses_cumulative_tax_state(batch, line)
            and self._batch_tax_period(batch).year == tax_year
            and self._batch_tax_period(batch).month > through_month
        }

    def _regular_payroll_dependency(
        self,
        org_id: uuid.UUID,
        employee: Employee,
        regular_batch_id: uuid.UUID,
        tax_period: YearMonth,
    ) -> tuple[PayrollBatch, PayrollLine, CumulativeTaxState]:
        """Load the only legal source of same-month combined-tax wage facts."""

        batch = self.session.scalar(
            select(PayrollBatch).where(
                PayrollBatch.id == regular_batch_id,
                PayrollBatch.org_id == org_id,
            )
        )
        if batch is None:
            raise CalculationValidationError(
                "REGULAR_PAYROLL_DEPENDENCY_NOT_FOUND",
                "regular_payroll_batch_id does not identify a batch in this organization",
            )
        if batch.batch_kind != PayrollBatchKind.REGULAR.value or batch.status != "posted":
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "combined annual bonus requires a posted regular payroll batch",
            )
        if self._batch_tax_period(batch) != tax_period:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "regular payroll must have the same tax period",
            )
        lines = self.session.scalars(
            select(PayrollLine).where(
                PayrollLine.payroll_batch_id == batch.id,
                PayrollLine.employee_id == employee.id,
            )
        ).all()
        if len(lines) != 1:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "referenced regular payroll must contain the same employee exactly once",
            )
        # The state-slot relation, not a replay of the caller's JSON request,
        # owns the legal regular -> combined-bonus dependency.  The database
        # validates the shape at commit; this read gives previews the same
        # deterministic early rejection without making JSON authoritative.
        slot = self.session.scalar(
            select(PayrollTaxStateSlot).where(
                PayrollTaxStateSlot.org_id == org_id,
                PayrollTaxStateSlot.employee_id == employee.id,
                PayrollTaxStateSlot.tax_year == tax_period.year,
                PayrollTaxStateSlot.tax_month == tax_period.month,
                PayrollTaxStateSlot.regular_batch_id == batch.id,
            )
        )
        if slot is None:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "referenced regular payroll has no formal tax-state slot",
            )
        if slot.final_batch_id != batch.id:
            raise CalculationValidationError(
                "DUPLICATE_COMBINED_BONUS_TAX_STATE",
                "a combined annual bonus already owns the final state for this regular payroll",
            )
        snapshot = next(
            (
                value
                for value in batch.calculation_input.get("employee_snapshots", [])
                if value.get("employee_id") == str(employee.id)
            ),
            None,
        )
        prior_state = self._tax_state_from_dict(
            snapshot.get("prior_tax_state") if snapshot else None
        )
        if prior_state is None:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "referenced regular payroll has no immutable prior tax state",
            )
        if prior_state.tax_year != tax_period.year or (
            prior_state.through_period is not None and prior_state.through_period >= tax_period
        ):
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "referenced regular payroll has an incompatible prior tax state",
            )
        return batch, lines[0], prior_state

    def _regular_tax_input_from_posted_line(
        self, employee: Employee, batch: PayrollBatch, line: PayrollLine
    ) -> CumulativeTaxPeriodInput:
        """Derive combined-tax facts solely from immutable regular payroll data."""

        request_items = batch.calculation_input.get("request", {}).get("employee_items", [])
        request_item = next(
            (value for value in request_items if value.get("employee_id") == str(employee.id)),
            None,
        )
        if request_item is None:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "referenced regular payroll has no immutable employee facts",
            )
        snapshot = next(
            (
                value
                for value in batch.calculation_input.get("employee_snapshots", [])
                if value.get("employee_id") == str(employee.id)
            ),
            None,
        )
        treatment_snapshot = (
            snapshot.get("first_wage_tax_treatment") if snapshot is not None else None
        )
        return CumulativeTaxPeriodInput(
            income_date=self._batch_tax_period(batch).end_date,
            withholding_start_date=self._required_tax_withholding_start_date(employee),
            income_fen=line.gross_salary_fen,
            tax_exempt_income_fen=0,
            employee_contributions_fen=(
                line.employee_social_insurance_fen + line.employee_housing_fund_fen
            ),
            special_additional_deduction_fen=line.special_additional_deduction_fen,
            other_legal_deduction_fen=line.other_legal_deduction_fen,
            tax_relief_fen=int(request_item.get("tax_relief_fen", 0)),
            standard_deduction_start_month=(
                int(treatment_snapshot["standard_deduction_start_month"])
                if treatment_snapshot is not None
                and treatment_snapshot.get("standard_deduction_start_month") is not None
                else None
            ),
        )

    def _posted_regular_tax_state_for_month(
        self, org_id: uuid.UUID, employee_id: uuid.UUID, tax_period: YearMonth
    ) -> bool:
        return (
            self.session.scalar(
                select(PayrollTaxStateSlot.id).where(
                    PayrollTaxStateSlot.org_id == org_id,
                    PayrollTaxStateSlot.employee_id == employee_id,
                    PayrollTaxStateSlot.tax_year == tax_period.year,
                    PayrollTaxStateSlot.tax_month == tax_period.month,
                )
            )
            is not None
        )

    def _employee_for_org(self, org_id: uuid.UUID, employee_id: uuid.UUID) -> Employee | None:
        return self.session.scalar(
            select(Employee).where(Employee.org_id == org_id, Employee.id == employee_id)
        )

    def _effective_profile(
        self, employee_id: uuid.UUID, on_date: date
    ) -> EmployeePayrollProfileVersion | None:
        successor = aliased(EmployeePayrollProfileVersion)
        matches = self.session.scalars(
            select(EmployeePayrollProfileVersion)
            .where(
                EmployeePayrollProfileVersion.employee_id == employee_id,
                EmployeePayrollProfileVersion.effective_from <= on_date,
                (EmployeePayrollProfileVersion.effective_to.is_(None))
                | (EmployeePayrollProfileVersion.effective_to >= on_date),
                ~exists(
                    select(successor.id).where(
                        successor.supersedes_id == EmployeePayrollProfileVersion.id
                    )
                ),
            )
            .order_by(
                EmployeePayrollProfileVersion.effective_from.desc(),
                EmployeePayrollProfileVersion.id,
            )
        ).all()
        if len(matches) > 1:
            raise CalculationValidationError(
                "AMBIGUOUS_EMPLOYEE_PROFILE", "more than one employee payroll profile is effective"
            )
        return matches[0] if matches else None

    @staticmethod
    def _employee_active_for_period(employee: Employee, period: YearMonth) -> bool:
        month_start = date(period.year, period.month, 1)
        return (
            employee.status == "active"
            and employee.employment_start_date <= period.end_date
            and (
                employee.employment_end_date is None or employee.employment_end_date >= month_start
            )
        )

    @staticmethod
    def _batch_tax_period(batch: PayrollBatch) -> YearMonth:
        if batch.batch_kind == PayrollBatchKind.REGULAR.value:
            return YearMonth(int(batch.payroll_period[:4]), int(batch.payroll_period[5:]))
        return YearMonth(batch.payment_date.year, batch.payment_date.month)

    @staticmethod
    def _required_tax_withholding_start_date(employee: Employee) -> date:
        if employee.tax_withholding_start_date is None:
            raise CalculationValidationError(
                "TAX_WITHHOLDING_START_DATE_REQUIRED",
                "tax withholding start date is required for cumulative tax calculation",
            )
        return employee.tax_withholding_start_date

    def _prior_tax_state(self, employee: Employee, period: YearMonth) -> CumulativeTaxState | None:
        """Read the preceding cumulative state through its formal state slot.

        A trace records the calculated numeric state, but it is not the relation
        that decides which batch owns a tax month.  The normalized state slot is
        that relation: its ``final_batch_id`` is either the regular payroll or
        the one legal combined bonus.  This prevents an old request JSON from
        being reinterpreted as a dependency during a later preview or reversal.
        """

        slots = self.session.scalars(
            select(PayrollTaxStateSlot)
            .where(
                PayrollTaxStateSlot.org_id == employee.org_id,
                PayrollTaxStateSlot.employee_id == employee.id,
                PayrollTaxStateSlot.tax_year == period.year,
                PayrollTaxStateSlot.tax_month < period.month,
            )
            .order_by(PayrollTaxStateSlot.tax_month.desc())
        ).all()
        posted_state: CumulativeTaxState | None = None
        posted_month: YearMonth | None = None
        if slots:
            slot = slots[0]
            row = self.session.execute(
                select(PayrollLine, PayrollBatch)
                .join(PayrollBatch, PayrollBatch.id == PayrollLine.payroll_batch_id)
                .where(
                    PayrollLine.org_id == employee.org_id,
                    PayrollLine.payroll_batch_id == slot.final_batch_id,
                    PayrollLine.employee_id == employee.id,
                    PayrollBatch.org_id == employee.org_id,
                    PayrollBatch.status == "posted",
                )
            ).all()
            if len(row) != 1:
                raise CalculationValidationError(
                    "INVALID_CUMULATIVE_TAX_STATE_SLOT",
                    "the formal tax-state slot has no single posted final payroll line",
                )
            line, batch = row[0]
            posted_month = self._batch_tax_period(batch)
            state = self._tax_state_from_trace(line.calculation_trace)
            if (
                state is None
                or posted_month.month != slot.tax_month
                or posted_month.year != slot.tax_year
                or state.tax_year != period.year
                or state.through_period != posted_month
            ):
                raise CalculationValidationError(
                    "INVALID_CUMULATIVE_TAX_STATE_SLOT",
                    "the formal tax-state slot and its final calculation disagree",
                )
            posted_state = state

        opening_successor = aliased(PayrollOpeningState)
        openings = self.session.scalars(
            select(PayrollOpeningState)
            .where(
                PayrollOpeningState.employee_id == employee.id,
                PayrollOpeningState.tax_year == period.year,
                PayrollOpeningState.through_month < period.month,
                ~exists(
                    select(opening_successor.id).where(
                        opening_successor.supersedes_id == PayrollOpeningState.id
                    )
                ),
            )
            .order_by(PayrollOpeningState.through_month.desc())
        ).all()
        opening = openings[0] if openings else None
        opening_state = None
        if opening is not None:
            opening_state = CumulativeTaxState(
                tax_year=opening.tax_year,
                through_period=YearMonth(opening.tax_year, opening.through_month),
                cumulative_income_fen=opening.cumulative_income_fen,
                cumulative_tax_exempt_income_fen=opening.cumulative_tax_exempt_income_fen,
                cumulative_standard_deduction_fen=opening.cumulative_basic_deduction_fen,
                cumulative_employee_contributions_fen=(
                    opening.cumulative_employee_social_insurance_fen
                    + opening.cumulative_employee_housing_fund_fen
                ),
                cumulative_special_additional_deduction_fen=(
                    opening.cumulative_special_additional_deduction_fen
                ),
                cumulative_other_legal_deduction_fen=opening.cumulative_other_legal_deduction_fen,
                cumulative_tax_relief_fen=opening.cumulative_tax_relief_fen,
                cumulative_withheld_tax_fen=opening.cumulative_tax_withheld_fen,
            )
        if posted_state is not None and opening_state is not None:
            if posted_month is None:
                raise AssertionError("posted tax state must have a tax month")
            if posted_month.month == opening.through_month:
                raise CalculationValidationError(
                    "AMBIGUOUS_CUMULATIVE_TAX_DEPENDENCY",
                    "an opening state and posted tax state cover the same tax month",
                )
            return posted_state if posted_month.month > opening.through_month else opening_state
        if posted_state is not None:
            return posted_state
        if opening_state is not None:
            return opening_state
        if period.month == 1 or (
            employee.tax_withholding_start_date is not None
            and employee.tax_withholding_start_date.year == period.year
        ):
            return CumulativeTaxState.empty(period.year)
        return None

    @staticmethod
    def _tax_state_dict(state: CumulativeTaxState | None) -> dict[str, Any] | None:
        if state is None:
            return None
        return {
            "tax_year": state.tax_year,
            "through_period": str(state.through_period) if state.through_period else None,
            "cumulative_income_fen": state.cumulative_income_fen,
            "cumulative_tax_exempt_income_fen": state.cumulative_tax_exempt_income_fen,
            "cumulative_standard_deduction_fen": state.cumulative_standard_deduction_fen,
            "cumulative_employee_contributions_fen": state.cumulative_employee_contributions_fen,
            "cumulative_special_additional_deduction_fen": (
                state.cumulative_special_additional_deduction_fen
            ),
            "cumulative_other_legal_deduction_fen": state.cumulative_other_legal_deduction_fen,
            "cumulative_tax_relief_fen": state.cumulative_tax_relief_fen,
            "cumulative_withheld_tax_fen": state.cumulative_withheld_tax_fen,
        }

    @staticmethod
    def _tax_input_dict(value: CumulativeTaxPeriodInput) -> dict[str, Any]:
        return {
            "income_date": value.income_date.isoformat(),
            "withholding_start_date": value.withholding_start_date.isoformat(),
            "income_fen": value.income_fen,
            "tax_exempt_income_fen": value.tax_exempt_income_fen,
            "employee_contributions_fen": value.employee_contributions_fen,
            "special_additional_deduction_fen": value.special_additional_deduction_fen,
            "other_legal_deduction_fen": value.other_legal_deduction_fen,
            "tax_relief_fen": value.tax_relief_fen,
            "standard_deduction_start_month": value.standard_deduction_start_month,
        }

    @classmethod
    def _tax_state_from_dict(cls, values: dict[str, Any] | None) -> CumulativeTaxState | None:
        if not values:
            return None
        period_value = values.get("through_period")
        through_period = (
            YearMonth(int(period_value[:4]), int(period_value[5:])) if period_value else None
        )
        return CumulativeTaxState(
            tax_year=int(values["tax_year"]),
            through_period=through_period,
            cumulative_income_fen=int(values["cumulative_income_fen"]),
            cumulative_tax_exempt_income_fen=int(values["cumulative_tax_exempt_income_fen"]),
            cumulative_standard_deduction_fen=int(values["cumulative_standard_deduction_fen"]),
            cumulative_employee_contributions_fen=int(
                values["cumulative_employee_contributions_fen"]
            ),
            cumulative_special_additional_deduction_fen=int(
                values["cumulative_special_additional_deduction_fen"]
            ),
            cumulative_other_legal_deduction_fen=int(
                values["cumulative_other_legal_deduction_fen"]
            ),
            cumulative_tax_relief_fen=int(values["cumulative_tax_relief_fen"]),
            cumulative_withheld_tax_fen=int(values["cumulative_withheld_tax_fen"]),
        )

    @classmethod
    def _tax_state_from_trace(cls, trace: list[dict[str, Any]]) -> CumulativeTaxState | None:
        state_entry = next(
            (entry for entry in reversed(trace) if entry.get("step") == "tax_state_after"), None
        )
        values = state_entry.get("values") if state_entry else None
        if not values:
            return None
        return cls._tax_state_from_dict(values)

    def _regular_prepared_line(
        self,
        employee: Employee,
        profile: EmployeePayrollProfileVersion,
        item: Any,
        result: Any,
        tax_state: CumulativeTaxState,
    ) -> dict[str, Any]:
        burden = result.contribution_burden_result
        social_employee = burden.employee_social_insurance_items
        social_employer = burden.employer_social_insurance_items
        housing_employee = burden.employee_housing_fund_items
        housing_employer = burden.employer_housing_fund_items
        trace = [
            *self._trace_dicts(result.contribution_result.trace),
            *self._trace_dicts(burden.trace),
            *self._trace_dicts(result.income_tax_result.trace),
            *self._trace_dicts(result.trace),
            {
                "step": "wage_tax_reporting_reconciliation",
                "values": {
                    "accounting_gross_salary_fen": result.gross_salary_fen,
                    "tax_reported_salary_fen": item.tax_reported_salary_fen,
                    "difference_fen": (
                        result.gross_salary_fen - (item.tax_reported_salary_fen or 0)
                    ),
                    "difference_reason": item.tax_reporting_difference_reason,
                },
            },
            {"step": "tax_state_after", "values": self._tax_state_dict(tax_state)},
        ]
        return {
            "employee_id": employee.id,
            "employee_payroll_profile_version_id": profile.id,
            "wage_tax_declaration_state": "declared",
            "tax_reported_salary_fen": item.tax_reported_salary_fen,
            "tax_reporting_difference_reason": item.tax_reporting_difference_reason,
            "special_additional_deduction_fen": item.special_additional_deduction_fen,
            "other_legal_deduction_fen": item.other_legal_deduction_fen,
            "annual_bonus_fen": 0,
            "employee_social_insurance_fen": result.employee_social_insurance_fen,
            "employer_social_insurance_fen": result.employer_social_insurance_fen,
            "employee_housing_fund_fen": result.employee_housing_fund_fen,
            "employer_housing_fund_fen": result.employer_housing_fund_fen,
            "employee_social_insurance_items": social_employee,
            "employer_social_insurance_items": social_employer,
            "employee_housing_fund_items": housing_employee,
            "employer_housing_fund_items": housing_employer,
            "individual_income_tax_fen": result.individual_income_tax_fen,
            "gross_salary_fen": result.gross_salary_fen,
            "net_salary_fen": result.net_pay_fen,
            "calculation_trace": trace,
        }

    def _unreported_regular_prepared_line(
        self,
        employee: Employee,
        profile: EmployeePayrollProfileVersion,
        item: Any,
        contribution: Any,
        burden: Any,
    ) -> dict[str, Any]:
        """Prepare a contribution-only line that creates no wage-tax fact or state slot."""

        trace = [
            *self._trace_dicts(contribution.trace),
            *self._trace_dicts(burden.trace),
            {
                "step": "wage_tax_not_declared",
                "values": {
                    "tax_reported_salary_fen": None,
                    "individual_income_tax_fen": 0,
                    "tax_state_advanced": False,
                },
            },
        ]
        return {
            "employee_id": employee.id,
            "employee_payroll_profile_version_id": profile.id,
            "wage_tax_declaration_state": "not_declared",
            "tax_reported_salary_fen": None,
            "tax_reporting_difference_reason": None,
            "special_additional_deduction_fen": item.special_additional_deduction_fen,
            "other_legal_deduction_fen": item.other_legal_deduction_fen,
            "annual_bonus_fen": 0,
            "employee_social_insurance_fen": burden.employee_social_insurance_fen,
            "employer_social_insurance_fen": burden.employer_social_insurance_fen,
            "employee_housing_fund_fen": burden.employee_housing_fund_fen,
            "employer_housing_fund_fen": burden.employer_housing_fund_fen,
            "employee_social_insurance_items": burden.employee_social_insurance_items,
            "employer_social_insurance_items": burden.employer_social_insurance_items,
            "employee_housing_fund_items": burden.employee_housing_fund_items,
            "employer_housing_fund_items": burden.employer_housing_fund_items,
            "individual_income_tax_fen": 0,
            "gross_salary_fen": 0,
            "net_salary_fen": 0,
            "calculation_trace": trace,
        }

    def _bonus_prepared_line(
        self,
        employee: Employee,
        profile: EmployeePayrollProfileVersion,
        item: Any,
        selected: Any,
        tax_state: CumulativeTaxState | None,
        *,
        regular_payroll_batch_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        trace: list[dict[str, Any]] = [
            {
                "step": "annual_bonus_selected_method",
                "values": {
                    "method": selected.method.value,
                    "tax_fen": selected.tax_fen or 0,
                    "net_bonus_fen": selected.net_bonus_fen or 0,
                    "regular_payroll_batch_id": (
                        str(regular_payroll_batch_id) if regular_payroll_batch_id else None
                    ),
                },
            }
        ]
        if tax_state is not None:
            trace.append({"step": "tax_state_after", "values": self._tax_state_dict(tax_state)})
        return {
            "employee_id": employee.id,
            "employee_payroll_profile_version_id": profile.id,
            "regular_payroll_batch_id": regular_payroll_batch_id,
            "wage_tax_declaration_state": "not_applicable",
            "tax_reported_salary_fen": None,
            "tax_reporting_difference_reason": None,
            "special_additional_deduction_fen": 0,
            "other_legal_deduction_fen": 0,
            "annual_bonus_fen": item.annual_bonus_fen,
            "employee_social_insurance_fen": 0,
            "employer_social_insurance_fen": 0,
            "employee_housing_fund_fen": 0,
            "employer_housing_fund_fen": 0,
            "employee_social_insurance_items": {},
            "employer_social_insurance_items": {},
            "employee_housing_fund_items": {},
            "employer_housing_fund_items": {},
            "individual_income_tax_fen": selected.tax_fen or 0,
            "gross_salary_fen": item.annual_bonus_fen,
            "net_salary_fen": selected.net_bonus_fen or 0,
            "calculation_trace": trace,
        }

    @staticmethod
    def _trace_dicts(trace: Any) -> list[dict[str, Any]]:
        return [{"step": entry.step, "values": entry.values} for entry in trace]

    @staticmethod
    def _bonus_scenario_dict(scenario: Any) -> dict[str, Any]:
        return {
            "method": scenario.method.value,
            "tax_fen": scenario.tax_fen,
            "net_bonus_fen": scenario.net_bonus_fen,
            "available": scenario.available,
            "unavailable_reason": scenario.unavailable_reason,
        }

    def _payroll_accrual_template(
        self, batch: PayrollBatch, lines: list[PayrollLine]
    ) -> tuple[list[Entry], list[OpenItemPlan]]:
        """The only payroll posting template; no caller supplies entry sides or account roles."""

        profiles = {
            profile.id: profile
            for profile in self.session.scalars(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.id.in_(
                        [line.employee_payroll_profile_version_id for line in lines]
                    )
                )
            ).all()
        }
        employees = {
            employee.id: employee
            for employee in self.session.scalars(
                select(Employee).where(Employee.id.in_([line.employee_id for line in lines]))
            ).all()
        }
        entries: list[Entry] = []
        plans: list[OpenItemPlan] = []
        totals = {
            "gross_salary": 0,
            "employer_social": 0,
            "employer_housing": 0,
        }
        statutory_amounts: dict[tuple[str, str], int] = {}
        for line in lines:
            profile = profiles.get(line.employee_payroll_profile_version_id)
            employee = employees.get(line.employee_id)
            if profile is None or employee is None:
                raise ValueError("payroll batch has an incomplete immutable employee snapshot")
            entries.extend(
                [
                    *(
                        [
                            Entry(
                                account_role=profile.expense_role,
                                debit_fen=line.gross_salary_fen,
                                counterparty_id=employee.counterparty_id,
                            )
                        ]
                        if line.gross_salary_fen
                        else []
                    ),
                    *(
                        [
                            Entry(
                                account_role=profile.expense_role,
                                debit_fen=(
                                    line.employer_social_insurance_fen
                                    + line.employer_housing_fund_fen
                                ),
                                counterparty_id=employee.counterparty_id,
                            )
                        ]
                        if line.employer_social_insurance_fen + line.employer_housing_fund_fen
                        else []
                    ),
                ]
            )
            if line.gross_salary_fen:
                plans.append(
                    OpenItemPlan(
                        counterparty_id=employee.counterparty_id,
                        item_type="payable",
                        original_amount_fen=line.gross_salary_fen,
                        due_date=batch.payment_date,
                        payable_category="salary",
                    )
                )
            totals["gross_salary"] += line.gross_salary_fen
            totals["employer_social"] += line.employer_social_insurance_fen
            totals["employer_housing"] += line.employer_housing_fund_fen
            for category, components in (
                ("employer_social", line.employer_social_insurance_items),
                ("employer_housing", line.employer_housing_fund_items),
            ):
                for insurance_kind, amount in components.items():
                    if amount:
                        key = (category, insurance_kind)
                        statutory_amounts[key] = statutory_amounts.get(key, 0) + int(amount)
        for role, total_key in (
            ("employee_salary_payable", "gross_salary"),
            ("employer_social_payable", "employer_social"),
            ("employer_housing_fund_payable", "employer_housing"),
        ):
            if totals[total_key]:
                entries.append(Entry(account_role=role, credit_fen=totals[total_key]))
        targets = self._payment_targets(batch.policy_snapshot.get("parameters", {}))
        for (category, insurance_kind), amount in statutory_amounts.items():
            target_key = "social_insurance" if "social" in category else "housing_fund"
            target = targets.get(target_key)
            if target is None:
                raise ValueError(f"missing statutory payment target for {target_key}")
            agency = self._agency_counterparty(batch.org_id, target)
            plans.append(
                OpenItemPlan(
                    counterparty_id=agency.id,
                    item_type="payable",
                    original_amount_fen=amount,
                    due_date=batch.payment_date,
                    payable_category=category,
                    payable_agency_code=target["agency_code"],
                    insurance_kind=insurance_kind,
                )
            )
        return entries, plans

    def _create_payroll_withholding_entitlements(
        self, batch: PayrollBatch, lines: list[PayrollLine]
    ) -> None:
        """Formalize every positive employee deduction before a batch becomes final."""
        for line in lines:
            for contribution_group, components in (
                ("employee_social_insurance", line.employee_social_insurance_items),
                ("employee_housing_fund", line.employee_housing_fund_items),
            ):
                for insurance_kind, amount in components.items():
                    if amount:
                        self.session.add(
                            PayrollWithholdingEntitlement(
                                org_id=batch.org_id,
                                payroll_line_id=line.id,
                                contribution_group=contribution_group,
                                insurance_kind=insurance_kind,
                                amount_fen=int(amount),
                            )
                        )
            if line.individual_income_tax_fen:
                self.session.add(
                    PayrollWithholdingEntitlement(
                        org_id=batch.org_id,
                        payroll_line_id=line.id,
                        contribution_group="individual_income_tax",
                        insurance_kind="individual_income_tax",
                        amount_fen=line.individual_income_tax_fen,
                    )
                )
        self.session.flush()

    @staticmethod
    def _payment_targets(parameters: dict[str, Any]) -> dict[str, dict[str, str]]:
        raw = parameters.get("payment_targets")
        if not isinstance(raw, dict):
            raise CalculationValidationError(
                "INVALID_POLICY_PARAMETERS", "payment_targets must be an object"
            )
        targets: dict[str, dict[str, str]] = {}
        for key in ("social_insurance", "housing_fund", "individual_income_tax"):
            value = raw.get(key)
            if (
                not isinstance(value, dict)
                or not value.get("agency_code")
                or not value.get("agency_name")
            ):
                raise CalculationValidationError(
                    "INVALID_POLICY_PARAMETERS",
                    f"payment_targets.{key} requires agency_code and agency_name",
                )
            targets[key] = {
                "agency_code": str(value["agency_code"]),
                "agency_name": str(value["agency_name"]),
            }
        return targets

    def _agency_counterparty(self, org_id: uuid.UUID, target: dict[str, str]) -> Counterparty:
        agency_code = target["agency_code"]
        legacy_name = f"法定缴费机构 {target['agency_name']}"
        coded_name = f"{legacy_name} [{agency_code}]"
        agencies = self.session.scalars(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == "other",
                Counterparty.external_ref == agency_code,
            )
        ).all()
        if len(agencies) > 1:
            raise ValueError("PAYROLL_AGENCY_COUNTERPARTY_CONFLICT")
        if agencies:
            if agencies[0].name not in {legacy_name, coded_name}:
                raise ValueError("PAYROLL_AGENCY_COUNTERPARTY_CONFLICT")
            return agencies[0]

        legacy = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == "other",
                Counterparty.name == legacy_name,
            )
        )
        if legacy is not None and legacy.external_ref == agency_code:
            return legacy

        name = coded_name
        statement = select(Counterparty).where(
            Counterparty.org_id == org_id,
            Counterparty.kind == "other",
            Counterparty.name == name,
        )
        agency = self.session.scalar(statement)
        if agency is None:
            # All payroll writers call this inside their transaction savepoint.
            # The nested savepoint makes the first shared statutory-agency
            # creation race replayable instead of poisoning the outer payroll
            # confirmation with uq_counterparty_identity.
            try:
                with self.session.begin_nested():
                    agency = Counterparty(
                        org_id=org_id,
                        kind="other",
                        name=name,
                        external_ref=agency_code,
                    )
                    self.session.add(agency)
                    self.session.flush()
            except IntegrityError:
                agency = self.session.scalar(statement)
                if agency is None:
                    raise ValueError("PAYROLL_AGENCY_COUNTERPARTY_CONFLICT") from None
        if agency.external_ref != agency_code:
            raise ValueError("PAYROLL_AGENCY_COUNTERPARTY_CONFLICT")
        return agency

    def _payroll_result_for_batch(
        self, batch: PayrollBatch, *, idempotent_replay: bool = False
    ) -> PayrollResult:
        lines = self.session.scalars(
            select(PayrollLine)
            .where(PayrollLine.payroll_batch_id == batch.id)
            .order_by(PayrollLine.id)
        ).all()
        event = (
            self.session.get(BusinessEvent, batch.business_event_id)
            if batch.business_event_id
            else None
        )
        voucher = event.vouchers[0] if event and event.vouchers else None
        status = PayrollResultStatus(batch.status)
        summary = {
            "gross_salary_fen": sum(line.gross_salary_fen for line in lines),
            "net_salary_fen": sum(line.net_salary_fen for line in lines),
            "employer_social_insurance_fen": sum(
                line.employer_social_insurance_fen for line in lines
            ),
            "employer_housing_fund_fen": sum(line.employer_housing_fund_fen for line in lines),
            "individual_income_tax_fen": sum(line.individual_income_tax_fen for line in lines),
        }
        return PayrollResult(
            status=status,
            batch_id=batch.id,
            calculation_hash=batch.calculation_hash,
            event_id=event.id if event else None,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            rule_version=batch.policy_snapshot.get("version"),
            trace=batch.calculation_trace,
            data={
                "batch_kind": batch.batch_kind,
                "payroll_period": batch.payroll_period,
                "version": batch.version,
                "tax_method": batch.tax_method,
                "summary": summary,
                "lines": [self._payroll_line_dict(line) for line in lines],
                "idempotent_replay": idempotent_replay,
            },
        )

    @staticmethod
    def _payroll_line_dict(line: PayrollLine) -> dict[str, Any]:
        return {
            "id": str(line.id),
            "employee_id": str(line.employee_id),
            "wage_tax_declaration_state": line.wage_tax_declaration_state,
            "tax_reported_salary_fen": line.tax_reported_salary_fen,
            "tax_reporting_difference_reason": line.tax_reporting_difference_reason,
            "annual_bonus_fen": line.annual_bonus_fen,
            "employee_social_insurance_fen": line.employee_social_insurance_fen,
            "employer_social_insurance_fen": line.employer_social_insurance_fen,
            "employee_social_insurance_items": line.employee_social_insurance_items,
            "employer_social_insurance_items": line.employer_social_insurance_items,
            "employee_housing_fund_fen": line.employee_housing_fund_fen,
            "employer_housing_fund_fen": line.employer_housing_fund_fen,
            "employee_housing_fund_items": line.employee_housing_fund_items,
            "employer_housing_fund_items": line.employer_housing_fund_items,
            "individual_income_tax_fen": line.individual_income_tax_fen,
            "gross_salary_fen": line.gross_salary_fen,
            "net_salary_fen": line.net_salary_fen,
            "trace": line.calculation_trace,
        }

    def preview_tax_period(self, request: TaxPeriodPreviewRequest) -> dict[str, Any]:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        if code := posting_period_error_code(
            self.session,
            request.org_id,
            request.adjustment_posting_date,
        ):
            return {"status": "rejected", "errors": [code]}
        try:
            result = calculate_tax_period(
                self.session,
                organization,
                request.start_date,
                request.end_date,
                request.adjustment_posting_date,
            )
        except ValueError as exc:
            code = str(exc)
            if code.startswith("TAX_"):
                return {"status": "rejected", "errors": [code]}
            raise
        return {"status": "calculated", **result.to_dict()}

    @staticmethod
    def _tax_period_confirm_payload_hash(request: TaxPeriodConfirmRequest) -> str:
        return FinanceService._canonical_payload_hash(
            {
                "command": "finance_confirm_tax_period",
                "org_id": str(request.org_id),
                "start_date": request.start_date.isoformat(),
                "end_date": request.end_date.isoformat(),
                "adjustment_posting_date": request.adjustment_posting_date.isoformat(),
                "calculation_hash": request.calculation_hash,
            }
        )

    def _tax_period_existing_result(self, event: BusinessEvent) -> FinanceResult:
        result = self._result_for_existing(event)
        tax_period = self.session.scalar(
            select(TaxPeriod).where(TaxPeriod.adjustment_event_id == event.id)
        )
        if tax_period is not None:
            result.data = {
                **result.data,
                "tax_period_id": str(tax_period.id),
                "calculation_hash": tax_period.calculation_hash,
                "idempotent_replay": True,
            }
        return result

    @staticmethod
    def _zero_tax_period_existing_result(
        confirmation: ZeroTaxPeriodConfirmation,
        *,
        idempotent_replay: bool,
    ) -> FinanceResult:
        return FinanceResult(
            status=ResultStatus.POSTED,
            rule_version=confirmation.rule_version,
            trace=list(confirmation.calculation.get("trace", [])),
            data={
                "zero_tax_period_confirmation_id": str(confirmation.id),
                "calculation_hash": confirmation.calculation_hash,
                "no_accounting_adjustment": True,
                "idempotent_replay": idempotent_replay,
            },
        )

    def _active_tax_period_conflict(
        self,
        request: TaxPeriodConfirmRequest,
        *,
        lock: bool,
    ) -> str | None:
        query = select(TaxPeriod).where(
            TaxPeriod.org_id == request.org_id,
            TaxPeriod.status == "posted",
            TaxPeriod.start_date <= request.end_date,
            TaxPeriod.end_date >= request.start_date,
        )
        if lock:
            query = query.with_for_update()
        active_periods = self.session.scalars(query).all()
        if any(
            period.start_date == request.start_date and period.end_date == request.end_date
            for period in active_periods
        ):
            return "TAX_PERIOD_ALREADY_POSTED"
        if active_periods:
            return "TAX_PERIOD_OVERLAP"
        return None

    def _assert_tax_period_range_constraint_now(self) -> None:
        """Surface the deferred PostgreSQL exclusion constraint in our savepoint."""

        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(text("SET CONSTRAINTS ex_tax_period_posted_range IMMEDIATE"))

    def _lock_tax_period_org(self, org_id: uuid.UUID) -> None:
        """Serialize a confirmation with taxable-source writes for one organization."""

        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('tax-period-org:' || :org_id, 0))"
                ),
                {"org_id": str(org_id)},
            )

    def confirm_tax_period(self, request: TaxPeriodConfirmRequest) -> FinanceResult:
        request_payload_hash = self._tax_period_confirm_payload_hash(request)
        existing_zero = self.session.scalar(
            select(ZeroTaxPeriodConfirmation).where(
                ZeroTaxPeriodConfirmation.org_id == request.org_id,
                ZeroTaxPeriodConfirmation.idempotency_key == request.idempotency_key,
            )
        )
        if existing_zero is not None:
            if existing_zero.request_payload_hash != request_payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return self._zero_tax_period_existing_result(
                existing_zero,
                idempotent_replay=True,
            )
        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            if existing.request_payload_hash != request_payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return self._tax_period_existing_result(existing)
        try:
            with self.session.begin_nested():
                return self._confirm_tax_period_write(
                    request,
                    request_payload_hash=request_payload_hash,
                )
        except AccountingPeriodError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[exc.code])
        except IntegrityError as exc:
            sqlstate, constraint_name, _primary_message = self._database_error_identity(exc)
            if constraint_name == "uq_zero_tax_confirmation_idempotency":
                existing_zero = self.session.scalar(
                    select(ZeroTaxPeriodConfirmation).where(
                        ZeroTaxPeriodConfirmation.org_id == request.org_id,
                        ZeroTaxPeriodConfirmation.idempotency_key == request.idempotency_key,
                    )
                )
                if existing_zero is None:
                    raise
                if existing_zero.request_payload_hash != request_payload_hash:
                    return FinanceResult(
                        status=ResultStatus.REJECTED,
                        errors=["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                    )
                return self._zero_tax_period_existing_result(
                    existing_zero,
                    idempotent_replay=True,
                )
            if (sqlstate, constraint_name) == ("23505", "uq_event_org_idempotency"):
                existing = self.session.scalar(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is None:
                    raise
                if existing.request_payload_hash != request_payload_hash:
                    return FinanceResult(
                        status=ResultStatus.REJECTED,
                        errors=["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                    )
                return self._tax_period_existing_result(existing)
            if (sqlstate, constraint_name) == ("23P01", "ex_tax_period_posted_range"):
                conflict = self._active_tax_period_conflict(request, lock=False)
                if conflict is None:
                    raise
                return FinanceResult(status=ResultStatus.REJECTED, errors=[conflict])
            raise

    def _confirm_tax_period_write(
        self,
        request: TaxPeriodConfirmRequest,
        *,
        request_payload_hash: str,
    ) -> FinanceResult:
        # Linearize organization tax configuration with both the confirmed
        # snapshot and taxable-source writes. Re-read under a row lock so a
        # previously cached Organization cannot validate a stale preview.
        self._lock_tax_period_org(request.org_id)
        existing_zero = self.session.scalar(
            select(ZeroTaxPeriodConfirmation).where(
                ZeroTaxPeriodConfirmation.org_id == request.org_id,
                ZeroTaxPeriodConfirmation.idempotency_key == request.idempotency_key,
            )
        )
        if existing_zero is not None:
            if existing_zero.request_payload_hash != request_payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return self._zero_tax_period_existing_result(
                existing_zero,
                idempotent_replay=True,
            )
        existing_event = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing_event is not None:
            if existing_event.request_payload_hash != request_payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return self._tax_period_existing_result(existing_event)
        organization = self.session.scalar(
            select(Organization)
            .where(Organization.id == request.org_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if organization is None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        try:
            tax_result = calculate_tax_period(
                self.session,
                organization,
                request.start_date,
                request.end_date,
                request.adjustment_posting_date,
            )
        except ValueError as exc:
            code = str(exc)
            if code.startswith("TAX_"):
                return FinanceResult(status=ResultStatus.REJECTED, errors=[code])
            raise
        if request.calculation_hash != tax_result.calculation_hash:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["TAX_PERIOD_CALCULATION_STALE"],
                rule_version=tax_result.rule_version,
                trace=tax_result.trace,
            )
        tax_profile = profile_as_of(
            self.session,
            org_id=request.org_id,
            as_of=request.start_date,
        )

        if conflict := self._active_tax_period_conflict(request, lock=True):
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=[conflict],
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
            confirmation = ZeroTaxPeriodConfirmation(
                org_id=request.org_id,
                start_date=request.start_date,
                end_date=request.end_date,
                adjustment_posting_date=request.adjustment_posting_date,
                idempotency_key=request.idempotency_key,
                request_payload_hash=request_payload_hash,
                rule_version=tax_result.rule_version,
                calculation=tax_result.to_dict(),
                calculation_hash=tax_result.calculation_hash,
                calculation_hash_payload=tax_result.calculation_hash_payload,
                filing_cycle_snapshot=tax_profile.filing_cycle,
                jurisdiction_snapshot=tax_profile.jurisdiction,
                urban_maintenance_rate_snapshot=Decimal(
                    format(tax_profile.urban_maintenance_rate, ".5f")
                ),
                vat_rule_id=uuid.UUID(tax_result.vat_rule_id),
                surtax_rule_id=uuid.UUID(tax_result.surtax_rule_id),
            )
            self.session.add(confirmation)
            self.session.flush()
            return self._zero_tax_period_existing_result(
                confirmation,
                idempotent_replay=False,
            )
        event = BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_payload_hash,
            event_type=EventType.TAX_RELIEF.value,
            status="draft",
            description=f"税务期间结算 {request.start_date} 至 {request.end_date}",
            facts={"tax_period": tax_result.to_dict()},
            business_date=request.end_date,
            tax_obligation_date=request.end_date,
            posting_date=request.adjustment_posting_date,
            rule_trace=tax_result.trace,
            rule_version=tax_result.rule_version,
        )
        self.session.add(event)
        self.session.flush()
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.adjustment_posting_date,
            description=event.description,
            entries=entries,
        )
        period_record = TaxPeriod(
            org_id=request.org_id,
            start_date=request.start_date,
            end_date=request.end_date,
            adjustment_posting_date=request.adjustment_posting_date,
            rule_version=tax_result.rule_version,
            calculation=tax_result.to_dict(),
            calculation_hash=tax_result.calculation_hash,
            calculation_hash_payload=tax_result.calculation_hash_payload,
            filing_cycle_snapshot=tax_profile.filing_cycle,
            jurisdiction_snapshot=tax_profile.jurisdiction,
            urban_maintenance_rate_snapshot=Decimal(
                format(tax_profile.urban_maintenance_rate, ".5f")
            ),
            vat_rule_id=uuid.UUID(tax_result.vat_rule_id),
            surtax_rule_id=uuid.UUID(tax_result.surtax_rule_id),
            adjustment_event_id=event.id,
        )
        self.session.add(period_record)
        self.session.flush()
        for source in tax_result.source_events:
            self.session.add(
                TaxPeriodSource(
                    org_id=request.org_id,
                    tax_period_id=period_record.id,
                    source_event_id=uuid.UUID(source["event_id"]),
                    gross_fen=source["gross_fen"],
                    net_fen=source["net_fen"],
                    vat_fen=source["vat_fen"],
                    exemption_eligible=source["exemption_eligible"],
                )
            )
        # Materialize the complete source snapshot while the adjustment event
        # is still draft. PostgreSQL seals this set when the event is posted.
        self.session.flush()
        event.status = "posted"
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action="tax_adjustment_posted",
                details={
                    "voucher_id": str(voucher.id),
                    "tax_period_id": str(period_record.id),
                    "calculation_hash": tax_result.calculation_hash,
                    "adjustment_posting_date": request.adjustment_posting_date.isoformat(),
                },
            )
        )
        self.session.flush()
        self._assert_tax_period_range_constraint_now()
        return FinanceResult(
            status=ResultStatus.POSTED,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            rule_version=tax_result.rule_version,
            trace=tax_result.trace,
            data={
                "tax_period_id": str(period_record.id),
                "calculation_hash": tax_result.calculation_hash,
                "idempotent_replay": False,
            },
        )

    def _create_payroll_reversal_batch(
        self,
        original_batch: PayrollBatch,
        reversal_event: BusinessEvent,
        request: ReverseEventRequest,
    ) -> PayrollBatch:
        """Create the immutable batch record that owns a payroll reversal voucher."""

        version = self._allocate_payroll_batch_version(
            original_batch.org_id,
            original_batch.batch_kind,
            original_batch.payroll_period,
        )
        entropy = (
            f"{original_batch.id}|{original_batch.calculation_hash}|{reversal_event.id}|"
            f"{request.idempotency_key}"
        )
        reversal_hash = hashlib.sha256(f"payroll-reversal|{entropy}".encode()).hexdigest()
        batch_idempotency_key = "payroll-reversal:" + hashlib.sha256(entropy.encode()).hexdigest()
        reversal_batch = PayrollBatch(
            org_id=original_batch.org_id,
            idempotency_key=batch_idempotency_key,
            batch_kind=original_batch.batch_kind,
            payroll_period=original_batch.payroll_period,
            version=version,
            status="draft",
            calculation_hash=reversal_hash,
            request_payload_hash=self._canonical_payload_hash(
                {
                    "kind": "payroll_reversal",
                    "original_batch_id": str(original_batch.id),
                    "reversal_event_id": str(reversal_event.id),
                    "idempotency_key": request.idempotency_key,
                }
            ),
            calculation_input={
                "reversal": {
                    "original_batch_id": str(original_batch.id),
                    "original_calculation_hash": original_batch.calculation_hash,
                    "reversal_event_id": str(reversal_event.id),
                    "reversal_event_idempotency_key": request.idempotency_key,
                    "reason": request.reason,
                    "posting_date": request.posting_date.isoformat(),
                }
            },
            calculation_trace=[
                {
                    "stage": "payroll_batch_reversal",
                    "original_batch_id": str(original_batch.id),
                    "reversal_event_id": str(reversal_event.id),
                    "reason": request.reason,
                }
            ],
            policy_snapshot=original_batch.policy_snapshot,
            policy_version_id=original_batch.policy_version_id,
            posting_date=request.posting_date,
            payment_date=original_batch.payment_date,
            tax_method=original_batch.tax_method,
            confirmed_by=None,
            confirmation_note=request.reason,
            confirmed_at=datetime.now(UTC),
            business_event_id=reversal_event.id,
            reversal_of_batch_id=original_batch.id,
        )
        self.session.add(reversal_batch)
        self.session.flush()
        original_evidence_ids = self.session.scalars(
            select(PayrollBatchEvidence.evidence_id).where(
                PayrollBatchEvidence.org_id == original_batch.org_id,
                PayrollBatchEvidence.payroll_batch_id == original_batch.id,
            )
        ).all()
        self._attach_payroll_batch_evidence(reversal_batch, original_evidence_ids)
        source_lines = self.session.scalars(
            select(PayrollLine)
            .where(
                PayrollLine.org_id == original_batch.org_id,
                PayrollLine.payroll_batch_id == original_batch.id,
            )
            .order_by(PayrollLine.id)
        ).all()
        if not source_lines:
            raise ValueError("payroll reversal requires immutable source payroll lines")
        reversal_line_by_source_id: dict[uuid.UUID, PayrollLine] = {}
        for source in source_lines:
            reversal_line = PayrollLine(
                org_id=original_batch.org_id,
                payroll_batch_id=reversal_batch.id,
                employee_id=source.employee_id,
                employee_payroll_profile_version_id=source.employee_payroll_profile_version_id,
                wage_tax_declaration_state=source.wage_tax_declaration_state,
                tax_reported_salary_fen=source.tax_reported_salary_fen,
                tax_reporting_difference_reason=source.tax_reporting_difference_reason,
                special_additional_deduction_fen=source.special_additional_deduction_fen,
                other_legal_deduction_fen=source.other_legal_deduction_fen,
                annual_bonus_fen=source.annual_bonus_fen,
                employee_social_insurance_fen=source.employee_social_insurance_fen,
                employer_social_insurance_fen=source.employer_social_insurance_fen,
                employee_housing_fund_fen=source.employee_housing_fund_fen,
                employer_housing_fund_fen=source.employer_housing_fund_fen,
                employee_social_insurance_items=source.employee_social_insurance_items,
                employer_social_insurance_items=source.employer_social_insurance_items,
                employee_housing_fund_items=source.employee_housing_fund_items,
                employer_housing_fund_items=source.employer_housing_fund_items,
                individual_income_tax_fen=source.individual_income_tax_fen,
                gross_salary_fen=source.gross_salary_fen,
                net_salary_fen=source.net_salary_fen,
                calculation_trace=[
                    *source.calculation_trace,
                    {
                        "stage": "payroll_reversal_line",
                        "original_payroll_line_id": str(source.id),
                    },
                ],
            )
            reversal_line_by_source_id[source.id] = reversal_line
            self.session.add(reversal_line)
        self.session.flush()
        original_entitlements = self.session.scalars(
            select(PayrollWithholdingEntitlement)
            .where(
                PayrollWithholdingEntitlement.org_id == original_batch.org_id,
                PayrollWithholdingEntitlement.payroll_line_id.in_(
                    reversal_line_by_source_id.keys()
                ),
            )
            .order_by(
                PayrollWithholdingEntitlement.payroll_line_id,
                PayrollWithholdingEntitlement.contribution_group,
                PayrollWithholdingEntitlement.insurance_kind,
            )
        ).all()
        for entitlement in original_entitlements:
            self.session.add(
                PayrollWithholdingEntitlement(
                    org_id=original_batch.org_id,
                    payroll_line_id=reversal_line_by_source_id[entitlement.payroll_line_id].id,
                    contribution_group=entitlement.contribution_group,
                    insurance_kind=entitlement.insurance_kind,
                    amount_fen=entitlement.amount_fen,
                )
            )
        self.session.flush()
        # The copied evidence and lines are complete before the draft is
        # sealed.  It becomes a final posted reversal only with its event.
        reversal_batch.status = "calculated"
        self.session.flush()
        return reversal_batch

    def reverse_event(self, request: ReverseEventRequest) -> FinanceResult:
        """Reverse through the common payroll idempotency/savepoint envelope."""

        # The public Python service is itself an API boundary. Route specialized
        # lifecycles here as well as in MCP so a caller cannot bypass their
        # downstream-first reversal dependency checks by instantiating the base
        # service directly. Subclasses call back into this method after their
        # own idempotency handling, so dispatch only for the concrete base type.
        if type(self) is FinanceService:
            event_type = self.session.scalar(
                select(BusinessEvent.event_type).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.id == request.event_id,
                )
            )
            if event_type in {
                "intangible_asset_acquisition",
                "intangible_asset_amortization",
                "intangible_asset_retirement",
            }:
                from .intangible_asset_service import IntangibleAssetService

                return IntangibleAssetService(self.session).reverse_event(request)
            if event_type in {
                "borrowing_drawdown",
                "borrowing_interest_accrual",
                "borrowing_interest_payment",
                "borrowing_principal_repayment",
            }:
                from .borrowing_service import BorrowingService

                return BorrowingService(self.session).reverse_event(request)
            if event_type in {
                "fixed_asset_acquisition",
                "fixed_asset_activation",
                "fixed_asset_depreciation",
                "fixed_asset_disposal",
            }:
                from .fixed_asset_service import FixedAssetService

                return FixedAssetService(self.session).reverse_event(request)
            if event_type in {
                "labor_remuneration_accrual",
                "unified_payout_run",
                "labor_withholding_tax_payment",
            }:
                from .labor_remuneration_service import LaborRemunerationService

                try:
                    with self.session.begin_nested():
                        return LaborRemunerationService(self.session).reverse_event(request)
                except AccountingPeriodError as exc:
                    return FinanceResult(status=ResultStatus.REJECTED, errors=[exc.code])
                except IntegrityError:
                    return FinanceResult(
                        status=ResultStatus.REJECTED,
                        errors=["LABOR_REVERSAL_CONCURRENT_WRITE_CONFLICT"],
                    )

        request_payload_hash = self._request_payload_hash(request)
        try:
            with self.session.begin_nested():
                return self._reverse_event_write(request)
        except AccountingPeriodError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[exc.code])
        except IntegrityError:
            existing = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None:
                if error := self._idempotency_error(
                    existing, request_payload_hash, payroll_envelope=True
                ):
                    return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
                return self._result_for_existing(existing)
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )

    def _reverse_event_write(self, request: ReverseEventRequest) -> FinanceResult:
        request_payload_hash = self._request_payload_hash(request)
        existing = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            if error := self._idempotency_error(
                existing, request_payload_hash, payroll_envelope=True
            ):
                return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
            return self._result_for_existing(existing)
        original = self.session.scalar(
            select(BusinessEvent)
            .where(BusinessEvent.id == request.event_id, BusinessEvent.org_id == request.org_id)
            .with_for_update()
        )
        if original is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["EVENT_NOT_FOUND"])
        # A competing reversal of the same source serializes on ``original``.
        # Re-read the idempotency key only after that lock is acquired: the
        # winner may have committed while this transaction was waiting.
        existing_after_lock = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.idempotency_key == request.idempotency_key,
            )
        )
        if existing_after_lock is not None:
            if error := self._idempotency_error(
                existing_after_lock, request_payload_hash, payroll_envelope=True
            ):
                return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
            return self._result_for_existing(existing_after_lock)
        if original.status != "posted" or original.reversed_by_event_id:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["EVENT_IS_NOT_REVERSIBLE"])
        dependent_children = self.session.scalars(
            select(BusinessEvent)
            .join(
                BusinessEventDependency,
                (BusinessEventDependency.org_id == BusinessEvent.org_id)
                & (BusinessEventDependency.child_event_id == BusinessEvent.id),
            )
            .where(
                BusinessEventDependency.org_id == request.org_id,
                BusinessEventDependency.parent_event_id == original.id,
                BusinessEvent.status == "posted",
            )
            .order_by(BusinessEvent.id)
            .with_for_update()
        ).all()
        if dependent_children:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["REVERSE_DEPENDENT_EVENTS_FIRST"],
            )
        locked_tax_source = self.session.scalar(
            select(TaxPeriodSource.source_event_id)
            .join(
                TaxPeriod,
                (TaxPeriod.org_id == TaxPeriodSource.org_id)
                & (TaxPeriod.id == TaxPeriodSource.tax_period_id),
            )
            .where(
                TaxPeriodSource.org_id == request.org_id,
                TaxPeriodSource.source_event_id == original.id,
                TaxPeriod.status == "posted",
            )
        )
        if locked_tax_source is not None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["TAX_PERIOD_SOURCE_LOCKED"],
            )
        original_voucher = self.session.scalar(
            select(Voucher).where(Voucher.event_id == original.id)
        )
        if original_voucher is None:
            return FinanceResult(status=ResultStatus.REJECTED, errors=["VOUCHER_NOT_FOUND"])
        original_evidence_ids = self.session.scalars(
            select(event_evidence.c.evidence_id)
            .where(
                event_evidence.c.org_id == request.org_id,
                event_evidence.c.event_id == original.id,
                event_evidence.c.relation_kind.in_(("supporting", "inherited")),
            )
            .order_by(event_evidence.c.evidence_id)
        ).all()

        source_items = self.session.scalars(
            select(OpenItem).where(OpenItem.source_event_id == original.id).with_for_update()
        ).all()
        if any(item.settled_amount_fen > 0 for item in source_items):
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["REVERSE_SETTLEMENT_EVENTS_BEFORE_SOURCE_EVENT"],
            )
        payroll_batch = None
        if original.event_type == "payroll_accrual":
            payroll_batch = self.session.scalar(
                select(PayrollBatch)
                .where(PayrollBatch.business_event_id == original.id)
                .with_for_update()
            )
            if payroll_batch is None:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["PAYROLL_BATCH_NOT_FOUND_FOR_ACCRUAL"],
                )
            active_supplement = self.session.scalar(
                select(PayrollContributionSupplement.id)
                .join(
                    BusinessEvent,
                    (BusinessEvent.org_id == PayrollContributionSupplement.org_id)
                    & (BusinessEvent.id == PayrollContributionSupplement.event_id),
                )
                .where(
                    PayrollContributionSupplement.org_id == request.org_id,
                    PayrollContributionSupplement.source_payroll_batch_id == payroll_batch.id,
                    BusinessEvent.status == "posted",
                )
            )
            if active_supplement is not None:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["REVERSE_DEPENDENT_EVENTS_FIRST"],
                )
            payroll_lines = self.session.scalars(
                select(PayrollLine).where(
                    PayrollLine.payroll_batch_id == payroll_batch.id
                )
            ).all()
            tax_employee_ids = [
                line.employee_id
                for line in payroll_lines
                if self._line_uses_cumulative_tax_state(payroll_batch, line)
            ]
            if tax_employee_ids:
                try:
                    payroll_tax_period = self._batch_tax_period(payroll_batch)
                    self._lock_payroll_tax_year(
                        payroll_batch.org_id,
                        tax_employee_ids,
                        payroll_tax_period.year,
                    )
                except CalculationValidationError as exc:
                    return FinanceResult(status=ResultStatus.REJECTED, errors=[exc.code])
            original_tax_period = self._batch_tax_period(payroll_batch)
            dependent = False
            slots: list[PayrollTaxStateSlot] = []
            if tax_employee_ids:
                dependent_rows = self.session.execute(
                    select(PayrollBatch, PayrollLine)
                    .join(PayrollLine, PayrollLine.payroll_batch_id == PayrollBatch.id)
                    .where(
                        PayrollBatch.org_id == original.org_id,
                        PayrollBatch.status == "posted",
                        PayrollBatch.reversal_of_batch_id.is_(None),
                        PayrollLine.employee_id.in_(tax_employee_ids),
                    )
                ).all()
                dependent = any(
                    candidate_batch.id != payroll_batch.id
                    and self._line_uses_cumulative_tax_state(
                        candidate_batch, candidate_line
                    )
                    and (
                        self._batch_tax_period(candidate_batch) > original_tax_period
                        or candidate_line.regular_payroll_batch_id == payroll_batch.id
                    )
                    for candidate_batch, candidate_line in dependent_rows
                )
                slots = self.session.scalars(
                    select(PayrollTaxStateSlot)
                    .where(
                        PayrollTaxStateSlot.org_id == payroll_batch.org_id,
                        PayrollTaxStateSlot.employee_id.in_(tax_employee_ids),
                        PayrollTaxStateSlot.tax_year == original_tax_period.year,
                        PayrollTaxStateSlot.tax_month == original_tax_period.month,
                    )
                    .with_for_update()
                ).all()
            if dependent:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["REVERSE_DEPENDENT_PAYROLL_BATCHES_FIRST"],
                )
            if payroll_batch.batch_kind == PayrollBatchKind.REGULAR.value and any(
                slot.regular_batch_id == payroll_batch.id
                and slot.final_batch_id != payroll_batch.id
                for slot in slots
            ):
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["REVERSE_DEPENDENT_PAYROLL_BATCHES_FIRST"],
                )

        reversal = BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_payload_hash,
            event_type="payroll_accrual" if payroll_batch is not None else "reversal",
            status="draft",
            description=f"冲正 {original.id}: {request.reason}",
            facts={
                "original_event_id": str(original.id),
                "reason": request.reason,
                "reversal": True,
                "payroll_batch_id": str(payroll_batch.id) if payroll_batch else None,
            },
            business_date=request.posting_date,
            posting_date=request.posting_date,
            rule_trace=[{"stage": "reversal", "original_event_id": str(original.id)}],
            rule_version=original.rule_version,
        )
        # Different source events do not share the original row lock. Keep
        # their common idempotency-key insertion inside a savepoint, so a
        # concurrent winner can be read back instead of poisoning the outer
        # transaction with a raw unique-constraint exception.
        try:
            with self.session.begin_nested():
                self.session.add(reversal)
                self.session.flush()
        except IntegrityError:
            existing_after_conflict = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if existing_after_conflict is not None:
                if error := self._idempotency_error(
                    existing_after_conflict, request_payload_hash, payroll_envelope=True
                ):
                    return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
                return self._result_for_existing(existing_after_conflict)
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )
        # Reversals preserve their source evidence as immutable inherited
        # links.  A separate, future reversal-reason attachment must use a
        # distinct Evidence object and the ``reversal_reason`` role.
        self._attach_evidence(reversal, original_evidence_ids, relation_kind="inherited")

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
        reversal_payroll_batch = None
        if payroll_batch is not None:
            reversal_payroll_batch = self._create_payroll_reversal_batch(
                payroll_batch, reversal, request
            )
            reversal.facts["payroll_reversal_batch_id"] = str(reversal_payroll_batch.id)
            if (
                payroll_batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value
                and payroll_batch.tax_method == AnnualBonusTaxMethod.COMBINED.value
            ):
                self.session.execute(
                    update(PayrollTaxStateSlot)
                    .where(
                        PayrollTaxStateSlot.org_id == payroll_batch.org_id,
                        PayrollTaxStateSlot.final_batch_id == payroll_batch.id,
                    )
                    .values(final_batch_id=PayrollTaxStateSlot.regular_batch_id)
                )
            elif payroll_batch.batch_kind == PayrollBatchKind.REGULAR.value:
                # A reversed regular payroll no longer owns a cumulative tax
                # month.  The database accepts this deletion only as part of
                # the linked reversal transition, and only after any combined
                # bonus has first restored ``final_batch_id`` to the regular.
                self.session.execute(
                    delete(PayrollTaxStateSlot).where(
                        PayrollTaxStateSlot.org_id == payroll_batch.org_id,
                        PayrollTaxStateSlot.regular_batch_id == payroll_batch.id,
                        PayrollTaxStateSlot.final_batch_id == payroll_batch.id,
                    )
                )
        # R5 requires the reversal provenance to be an exact structural
        # inverse of its payroll source.  An accrual owns the newly-created
        # reversal batch, while salary/statutory payments preserve every
        # source open-item edge (a payment may have multiple allocations).
        # Do not collapse that relation to one batch: the final-event
        # invariant deliberately rejects a partial or JSON-derived chain.
        if original.event_type == "payroll_accrual":
            if reversal_payroll_batch is None:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["PAYROLL_REVERSAL_BATCH_NOT_FOUND"],
                )
            self.session.add(
                PayrollEventLink(
                    org_id=request.org_id,
                    event_id=reversal.id,
                    payroll_batch_id=reversal_payroll_batch.id,
                    source_payment_event_id=original.id,
                    link_kind="reversal",
                )
            )
        elif original.event_type in {
            EventType.SALARY_PAYMENT.value,
            EventType.SOCIAL_INSURANCE_PAYMENT.value,
            EventType.HOUSING_FUND_PAYMENT.value,
            EventType.INDIVIDUAL_INCOME_TAX_PAYMENT.value,
        }:
            expected_link_kind = (
                "salary_payment"
                if original.event_type == EventType.SALARY_PAYMENT.value
                else "statutory_payment"
            )
            original_payment_links = self.session.scalars(
                select(PayrollEventLink)
                .where(
                    PayrollEventLink.org_id == request.org_id,
                    PayrollEventLink.event_id == original.id,
                    PayrollEventLink.link_kind == expected_link_kind,
                )
                .order_by(
                    PayrollEventLink.payroll_batch_id,
                    PayrollEventLink.source_open_item_id,
                    PayrollEventLink.id,
                )
            ).all()
            if not original_payment_links:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["PAYROLL_REVERSAL_SOURCE_LINK_NOT_FOUND"],
                )
            for original_link in original_payment_links:
                self.session.add(
                    PayrollEventLink(
                        org_id=request.org_id,
                        event_id=reversal.id,
                        payroll_batch_id=original_link.payroll_batch_id,
                        source_payment_event_id=original.id,
                        source_open_item_id=original_link.source_open_item_id,
                        link_kind="reversal",
                    )
                )
        # The reversal edge belongs to the still-draft reversal event.  Flush
        # it before promoting that event (and its payroll reversal batch) to
        # their immutable final states.
        self.session.flush()
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
            item.status = "open" if item.settled_amount_fen == 0 else "partial"
            settlement.reversed = True
            settlement.reversed_by_event_id = reversal.id
        active_bank_matches = self.session.scalars(
            select(BankTransactionMatch)
            .where(
                BankTransactionMatch.org_id == request.org_id,
                BankTransactionMatch.event_id == original.id,
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
            .with_for_update()
        ).all()
        if active_bank_matches:
            bank_rows = self.session.scalars(
                select(BankTransaction)
                .where(
                    BankTransaction.org_id == request.org_id,
                    BankTransaction.id.in_(
                        [match.bank_transaction_id for match in active_bank_matches]
                    ),
                )
                .with_for_update()
            ).all()
            bank_by_id = {row.id: row for row in bank_rows}
            for match in active_bank_matches:
                match.invalidated_by_event_id = reversal.id
                match.invalidated_at = datetime.now(UTC)
                bank_by_id[match.bank_transaction_id].matched_event_id = None
        if original.event_type == EventType.SALARY_PAYMENT.value:
            withholding_allocations = self.session.scalars(
                select(PayrollWithholdingPaymentAllocation)
                .where(
                    PayrollWithholdingPaymentAllocation.org_id == request.org_id,
                    PayrollWithholdingPaymentAllocation.payment_event_id == original.id,
                    PayrollWithholdingPaymentAllocation.reversed.is_(False),
                )
                .with_for_update()
            ).all()
            for allocation in withholding_allocations:
                allocation.reversed = True
                allocation.reversed_by_event_id = reversal.id
            actual_deductions = self.session.scalars(
                select(PayrollSalaryActualDeductionAllocation)
                .where(
                    PayrollSalaryActualDeductionAllocation.org_id == request.org_id,
                    PayrollSalaryActualDeductionAllocation.payment_event_id == original.id,
                    PayrollSalaryActualDeductionAllocation.reversed.is_(False),
                )
                .with_for_update()
            ).all()
            for deduction in actual_deductions:
                deduction.reversed = True
                deduction.reversed_by_event_id = reversal.id

        tax_period = self.session.scalar(
            select(TaxPeriod).where(TaxPeriod.adjustment_event_id == original.id)
        )
        if tax_period:
            tax_period.status = "reversed"
        original.status = "reversed"
        original.reversed_by_event_id = reversal.id
        if payroll_batch is not None:
            payroll_batch.status = "reversed"
            if payroll_batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value:
                usages = self.session.scalars(
                    select(AnnualBonusUsage).where(
                        AnnualBonusUsage.payroll_batch_id == payroll_batch.id
                    )
                ).all()
                for usage in usages:
                    self.session.delete(usage)
        if reversal_payroll_batch is not None:
            reversal_payroll_batch.status = "posted"
        reversal.status = "posted"
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=reversal.id,
                action="event_reversed",
                details={
                    "original_event_id": str(original.id),
                    "reason": request.reason,
                    "payroll_reversal_batch_id": (
                        str(reversal_payroll_batch.id) if reversal_payroll_batch else None
                    ),
                },
            )
        )
        return FinanceResult(
            status=ResultStatus.POSTED,
            event_id=reversal.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            trace=reversal.rule_trace,
        )
