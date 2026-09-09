from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import replace
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

from .component_schemas import RecordEventRequest
from .enterprise_income_tax import EnterpriseIncomeTaxService, lock_income_tax
from .ledger import (
    AccountingPeriodError,
    ComponentPostingPlan,
    Entry,
    OpenItemPlan,
    assert_period_open,
    build_business_event,
    commit_posting_plan,
    posting_period_error_code,
)
from .models import (
    Account,
    AnnualBonusUsage,
    AuditLog,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    BusinessEventComponent,
    BusinessEventDependency,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    LaborRemunerationBatch,
    LaborRemunerationEventLink,
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
    AnnualBonusTaxMethod,
    ConfirmPayrollRequest,
    FinanceResult,
    PayrollBatchKind,
    PayrollPolicyParameters,
    PayrollResult,
    PayrollResultStatus,
    PayrollWageTaxScope,
    PreviewPayrollRequest,
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
from .tax import calculate_tax_period
from .tax_accounts import vat_relief_entries


class FinanceService:
    DEFERRED_OUTPUT_VAT_RULE_VERSION = "mof-cai-kuai-2016-22-v1"
    DEFERRED_OUTPUT_VAT_RULE_SOURCE_URL = (
        "https://www.mof.gov.cn/gkml/caizhengwengao/2017wg/wg201703/201707/t20170707_2641107.htm"
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
        return self._canonical_payload_hash(
            request.model_dump(
                mode="json",
                exclude={"employee_items": {"__all__": {"tax_reporting_difference_reason"}}},
            )
        )

    @staticmethod
    def _request_payload_hash(request: Any) -> str:
        """Hash only caller-supplied business facts, before service derivation."""
        payload = request.model_dump(mode="json")
        if isinstance(request, ReverseEventRequest):
            payload.pop("reason", None)
        return FinanceService._canonical_payload_hash(payload)

    def _validate_posting_bank_account(self, org_id, code, settlement_date):
        from .coa import account_business_class, get_account_by_code

        account = get_account_by_code(self.session, org_id, code)
        if not account.active or account_business_class(account) not in {
            "bank",
            "payment_platform_funds",
        }:
            raise ValueError("INVALID_POSTING_BANK_ACCOUNT")
        return account

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
            return line.wage_tax_scope == "wage_income"
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
        from .component_service import ComponentService

        return ComponentService(self.session).record(request)

    def preview_event(self, request: RecordEventRequest) -> FinanceResult:
        from .component_service import ComponentService

        return ComponentService(self.session).preview(request)

    def _tax_obligation_date_is_locked(self, org_id: uuid.UUID, obligation_date: date) -> bool:
        """Return whether an active immutable tax snapshot owns this tax date."""

        if (
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
        ):
            return True
        active_confirmation_payments = self.session.scalars(
            select(BusinessEventComponent)
            .join(
                BusinessEvent,
                (BusinessEvent.org_id == BusinessEventComponent.org_id)
                & (BusinessEvent.id == BusinessEventComponent.event_id),
            )
            .where(
                BusinessEventComponent.org_id == org_id,
                BusinessEventComponent.kind == "tax_settlement",
                BusinessEvent.status == "posted",
            )
        )
        for component in active_confirmation_payments:
            if (
                component.facts.get("tax_type") != "vat"
                or component.facts.get("settlement_kind") != "payment"
            ):
                continue
            confirmation_id = component.derived.get("tax_confirmation_id")
            if not confirmation_id:
                continue
            confirmation = self.session.get(
                ZeroTaxPeriodConfirmation, uuid.UUID(str(confirmation_id))
            )
            if (
                confirmation is not None
                and confirmation.org_id == org_id
                and component.derived.get("tax_confirmation_hash") == confirmation.calculation_hash
                and confirmation.start_date <= obligation_date <= confirmation.end_date
            ):
                return True
        return False

    @staticmethod
    def _person_payable_role(counterparty: Counterparty | None) -> str:
        if counterparty is None or counterparty.kind not in {"employee", "owner"}:
            raise ValueError("person counterparty must be an employee or owner")
        return "owner_payable" if counterparty.kind == "owner" else "employee_payable"

    @staticmethod
    def _open_payable_account_role(item: OpenItem, counterparty: Counterparty) -> str:
        category_roles = {
            "pass_through": "pass_through_payable",
            "salary": "employee_salary_payable",
            "employer_social": "employer_social_payable",
            "withheld_employee_social": "withheld_employee_social_payable",
            "employer_housing": "employer_housing_fund_payable",
            "withheld_employee_housing": "withheld_employee_housing_fund_payable",
            "individual_income_tax": "individual_income_tax_payable",
            "labor_remuneration": "labor_remuneration_payable",
            "labor_individual_income_tax": "individual_income_tax_payable",
        }
        if item.payable_category is not None:
            try:
                return category_roles[item.payable_category]
            except KeyError as exc:
                raise ValueError(
                    f"unsupported payable category for payment on behalf: {item.payable_category}"
                ) from exc
        counterparty_roles = {
            "supplier": "accounts_payable",
            "employee": "employee_payable",
            "owner": "owner_payable",
        }
        try:
            return counterparty_roles[counterparty.kind]
        except KeyError as exc:
            raise ValueError(
                "an uncategorized payable must belong to a supplier, employee, or owner"
            ) from exc

    def derive_salary_settlement(self, org_id: uuid.UUID, request) -> dict[str, Any]:
        """Validate per-kind deductions against normalized payroll entitlements."""

        if not request.allocations:
            raise ValueError("salary payment requires allocations")
        allocation_by_item = {item.open_item_id: item.amount_fen for item in request.allocations}
        if len(allocation_by_item) != len(request.allocations):
            raise ValueError("salary payment cannot allocate an open item more than once")
        withholding_by_item = {item.open_item_id: item for item in request.withholding_allocations}
        if len(withholding_by_item) != len(request.withholding_allocations):
            raise ValueError("salary payment cannot state withholdings twice for one open item")
        if set(allocation_by_item) != set(withholding_by_item):
            raise ValueError(
                "salary payment needs explicit withholdings for every salary allocation"
            )
        actual_deduction_by_item = {
            item.open_item_id: item.amount_fen for item in request.actual_deduction_allocations
        }
        if len(actual_deduction_by_item) != len(request.actual_deduction_allocations):
            raise ValueError(
                "salary payment cannot state actual deductions twice for one open item"
            )
        if not set(actual_deduction_by_item).issubset(allocation_by_item):
            raise ValueError("salary actual deduction must belong to a salary allocation")

        batch_ids: set[uuid.UUID] = set()
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
                    OpenItem.org_id == org_id,
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
                    PayrollEventLink.org_id == org_id,
                    PayrollEventLink.event_id == open_item.source_event_id,
                    PayrollEventLink.component_id == open_item.source_component_id,
                    PayrollEventLink.link_kind == "payroll_accrual",
                )
            )
            if source_link is None:
                raise ValueError("salary open item does not originate from a payroll accrual")
            source_batch = self.session.scalar(
                select(PayrollBatch).where(
                    PayrollBatch.org_id == org_id,
                    PayrollBatch.id == source_link.payroll_batch_id,
                )
            )
            if source_batch is None:
                raise ValueError("salary payroll origin is not available in this organization")
            batch_ids.add(source_batch.id)
            line = self.session.scalar(
                select(PayrollLine)
                .join(Employee, Employee.id == PayrollLine.employee_id)
                .where(
                    PayrollLine.org_id == org_id,
                    PayrollLine.payroll_batch_id == source_batch.id,
                    Employee.org_id == org_id,
                    Employee.counterparty_id == open_item.counterparty_id,
                )
            )
            if line is None:
                raise ValueError("salary open item has no matching payroll line")
            profile = self.session.scalar(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.org_id == org_id,
                    EmployeePayrollProfileVersion.id == line.employee_payroll_profile_version_id,
                    EmployeePayrollProfileVersion.employee_id == line.employee_id,
                )
            )
            if profile is None:
                raise ValueError("salary payroll line has no matching payroll expense profile")
            supplied = withholding_by_item[open_item_id]
            entitlements = self.session.scalars(
                select(PayrollWithholdingEntitlement)
                .where(
                    PayrollWithholdingEntitlement.org_id == org_id,
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
                    actual_deduction_by_expense_role.get(profile.expense_role, 0) + actual_deduction
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
                    "payroll_batch_id": str(source_batch.id),
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
        if not batch_ids or cash_total != request.amount_fen:
            raise ValueError(
                "salary cash payment must equal gross allocations less explicit "
                "withholdings and actual salary deductions"
            )
        return {
            "payroll_batch_ids": sorted(str(batch_id) for batch_id in batch_ids),
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
        self, org_id: uuid.UUID, derived: dict[str, Any]
    ) -> list[OpenItemPlan]:
        plans: list[OpenItemPlan] = []
        for field_name, category in (
            ("employee_social_insurance_items", "withheld_employee_social"),
            ("employee_housing_fund_items", "withheld_employee_housing"),
        ):
            components: dict[str, int] = {}
            for allocation in derived["salary_withholding_allocations"]:
                for code, amount in allocation[field_name].items():
                    components[code] = components.get(code, 0) + int(amount)
            if not components:
                continue
            for insurance_kind, amount in components.items():
                if amount:
                    plans.append(
                        OpenItemPlan(
                            counterparty_id=None,
                            item_type="payable",
                            original_amount_fen=amount,
                            due_date=None,
                            payable_category=category,
                            insurance_kind=insurance_kind,
                        )
                    )
        tax_amount = int(derived["individual_income_tax_fen"])
        if tax_amount:
            plans.append(
                OpenItemPlan(
                    counterparty_id=None,
                    item_type="payable",
                    original_amount_fen=tax_amount,
                    due_date=None,
                    payable_category="individual_income_tax",
                )
            )
        return plans

    def _record_payroll_withholding_allocations(
        self, event: BusinessEvent, derived: dict[str, Any], *, component_id: uuid.UUID
    ) -> None:
        """Persist statutory and actual salary deductions against their payroll lines."""
        for allocation in derived.get("withholding_payment_allocations", []):
            self.session.add(
                PayrollWithholdingPaymentAllocation(
                    org_id=event.org_id,
                    entitlement_id=uuid.UUID(allocation["entitlement_id"]),
                    payment_event_id=event.id,
                    payment_component_id=component_id,
                    amount_fen=int(allocation["amount_fen"]),
                )
            )
        for allocation in derived.get("actual_salary_deduction_allocations", []):
            self.session.add(
                PayrollSalaryActualDeductionAllocation(
                    org_id=event.org_id,
                    payroll_line_id=uuid.UUID(allocation["payroll_line_id"]),
                    payment_event_id=event.id,
                    payment_component_id=component_id,
                    amount_fen=int(allocation["amount_fen"]),
                    expense_role=allocation["expense_role"],
                )
            )

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
        existing_ids = set(
            self.session.scalars(
                select(event_evidence.c.evidence_id).where(
                    event_evidence.c.org_id == event.org_id,
                    event_evidence.c.event_id == event.id,
                )
            )
        )
        evidence_ids = [identity for identity in evidence_ids if identity not in existing_ids]
        if not evidence_ids:
            return
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
        self.session.expire(event, ["evidence"])

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

    @staticmethod
    def _optional_date(value: Any) -> date | None:
        if value is None or isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    def _result_for_existing(self, event: BusinessEvent) -> FinanceResult:
        voucher = event.vouchers[0] if event.vouchers else None
        status = (
            ResultStatus.POSTED
            if event.status in {"posted", "reversed"}
            else ResultStatus(event.status)
        )
        created_open_items = self.session.scalars(
            select(OpenItem)
            .where(OpenItem.org_id == event.org_id, OpenItem.source_event_id == event.id)
            .order_by(OpenItem.id)
        ).all()
        data: dict[str, Any] = {
            "idempotent_replay": True,
            "original_status": event.status,
        }
        if isinstance(event.facts.get("derived"), dict):
            data["derived"] = event.facts["derived"]
        if created_open_items:
            data["created_open_items"] = [
                self._open_item_result(item) for item in created_open_items
            ]
        return FinanceResult(
            status=status,
            event_id=event.id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            trace=event.rule_trace,
            missing_information=event.facts.get("_decision", {}).get("missing", []),
            errors=event.facts.get("_decision", {}).get("errors", []),
            rule_version=event.rule_version,
            data=data,
        )

    @staticmethod
    def _open_item_result(item: OpenItem) -> dict[str, Any]:
        return {
            "open_item_id": str(item.id),
            "item_type": item.item_type,
            "original_amount_fen": item.original_amount_fen,
            "settled_amount_fen": item.settled_amount_fen,
            "status": item.status,
            "counterparty_id": str(item.counterparty_id) if item.counterparty_id else None,
            "payable_category": item.payable_category,
            "pass_through_key": item.pass_through_key,
            "pass_through_beneficiary_id": str(item.pass_through_beneficiary_id)
            if item.pass_through_beneficiary_id
            else None,
            "due_date": item.due_date.isoformat() if item.due_date else None,
        }

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
                    "housing_fund_participating": (existing_successor.housing_fund_participating),
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

        payload_hash = self._canonical_payload_hash(
            request.model_dump(
                mode="json", exclude={"confirmation_description", "declaration_date"}
            )
        )
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
        from .corrections import source_change_gate

        if correction_required := source_change_gate(self.session, request):
            return correction_required
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
        from .corrections import source_batches

        calculated_batches_to_supersede = source_batches(
            self.session, request, statuses=("calculated",)
        )
        treatment = PayrollFirstWageTaxTreatment(
            org_id=request.org_id,
            employee_id=request.employee_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            tax_year=request.tax_year,
            first_wage_month=request.first_wage_month,
            treatment_state=request.treatment_state.value,
            declaration_date=request.declaration_date,
            confirmation_description=request.confirmation_description or None,
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

        payload_hash = self._canonical_payload_hash(
            request.model_dump(
                mode="json", exclude={"reason_code", "reason_description", "declaration_date"}
            )
        )
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
        from .corrections import source_change_gate

        if correction_required := source_change_gate(self.session, request):
            return correction_required
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
        supplied_predecessors = (
            list(
                self.session.scalars(
                    select(PayrollContributionActualItem)
                    .where(
                        PayrollContributionActualItem.org_id == request.org_id,
                        PayrollContributionActualItem.id.in_(supersedes_ids),
                    )
                    .with_for_update()
                )
            )
            if supersedes_ids
            else []
        )
        if {item.id for item in supplied_predecessors} != supersedes_ids:
            return {"status": "rejected", "errors": ["INVALID_CONTRIBUTION_ACTUAL_SUPERSEDES"]}
        supplied_by_key = {
            (item.contribution_group, item.insurance_kind): item for item in supplied_predecessors
        }
        if any(
            item.employee_id != request.employee_id
            or item.contribution_period != request.contribution_period
            for item in supplied_predecessors
        ) or set(supplied_by_key) != {key for key in requested_by_key if key in active_by_key}:
            return {"status": "rejected", "errors": ["INVALID_CONTRIBUTION_ACTUAL_SUPERSEDES"]}
        for key, active in active_by_key.items():
            supplied = supplied_by_key.get(key)
            if key in requested_by_key and (supplied is None or supplied.id != active.id):
                return {
                    "status": "rejected",
                    "errors": ["CONTRIBUTION_ACTUAL_CORRECTION_REQUIRES_SUPERSEDES"],
                }
        from .corrections import source_batches

        calculated_batches_to_supersede = source_batches(
            self.session, request, statuses=("calculated",)
        )
        actual_set = PayrollContributionActualSet(
            org_id=request.org_id,
            employee_id=request.employee_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            contribution_period=request.contribution_period,
            declaration_date=request.declaration_date,
            reason_code=request.reason_code,
            reason_description=request.reason_description or None,
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
        """Compile one historical assessment through the component protocol."""
        from .component_schemas import RecordEventRequest
        from .component_service import ComponentService

        facts = request.model_dump(
            mode="json",
            exclude={
                "org_id",
                "idempotency_key",
                "posting_date",
                "evidence_references",
                "due_date",
                "assessment_reference",
                "reason_code",
                "reason_description",
            },
        )
        metadata = {
            key: value
            for key, value in {
                "due_date": request.due_date,
                "assessment_reference": request.assessment_reference,
                "reason_code": request.reason_code,
                "reason": request.reason_description or None,
            }.items()
            if value is not None
        }
        result = ComponentService(self.session).record(
            RecordEventRequest.model_validate(
                {
                    "org_id": request.org_id,
                    "idempotency_key": request.idempotency_key,
                    "posting_date": request.posting_date,
                    "description": "",
                    "evidence_references": request.evidence_references,
                    "components": [
                        {
                            "key": "supplement",
                            "kind": "payroll_contribution_supplement",
                            "business_date": request.posting_date,
                            "metadata": metadata,
                            **facts,
                        }
                    ],
                }
            )
        )
        if result.status == ResultStatus.POSTED:
            result.data["supplement_id"] = next(
                row["derived"]["supplement_id"]
                for row in result.data["components"]
                if row["kind"] == "payroll_contribution_supplement"
            )
        return result

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
            calculation = self._calculate_payroll(
                request,
                allow_calculated_regular=(
                    request.batch_kind == PayrollBatchKind.ANNUAL_BONUS
                    and request.tax_method == AnnualBonusTaxMethod.COMBINED
                ),
            )
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
                employee_ids = [item.employee_id for item in request.employee_items]
                overlapping_batch_ids = (
                    select(PayrollBatch.id)
                    .join(
                        PayrollLine,
                        (PayrollLine.org_id == PayrollBatch.org_id)
                        & (PayrollLine.payroll_batch_id == PayrollBatch.id),
                    )
                    .where(
                        PayrollBatch.org_id == request.org_id,
                        PayrollBatch.batch_kind == request.batch_kind.value,
                        PayrollBatch.payroll_period == request.payroll_period,
                        PayrollBatch.status == "calculated",
                        PayrollLine.employee_id.in_(employee_ids),
                    )
                )
                self.session.execute(
                    update(PayrollBatch)
                    .where(PayrollBatch.id.in_(overlapping_batch_ids))
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
                    payment_date=(
                        request.payment_date
                        if request.batch_kind == PayrollBatchKind.ANNUAL_BONUS
                        else None
                    ),
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
        except ValueError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[str(exc)])
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
        """Confirm a calculated batch through the common component protocol."""

        from .component_service import ComponentService

        batch = self.session.scalar(
            select(PayrollBatch).where(
                PayrollBatch.id == request.batch_id,
                PayrollBatch.org_id == request.org_id,
            )
        )
        if batch is None:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED, errors=["PAYROLL_BATCH_NOT_FOUND"]
            )
        evidence_ids = list(
            self.session.scalars(
                select(PayrollBatchEvidence.evidence_id)
                .where(
                    PayrollBatchEvidence.org_id == batch.org_id,
                    PayrollBatchEvidence.payroll_batch_id == batch.id,
                )
                .order_by(PayrollBatchEvidence.evidence_id)
            )
        )
        component_request = RecordEventRequest.model_validate(
            {
                "org_id": request.org_id,
                "idempotency_key": request.idempotency_key,
                "posting_date": batch.posting_date,
                "description": (
                    batch.calculation_input.get("request", {}).get("description") or "工资计提"
                ),
                "components": [
                    {
                        "key": "payroll_accrual",
                        "kind": "payroll_accrual",
                        "business_date": batch.posting_date,
                        "batch_id": batch.id,
                        "calculation_hash": request.calculation_hash,
                        "metadata": (
                            {"confirmation_note": request.confirmation_note}
                            if request.confirmation_note
                            else {}
                        ),
                        "evidence_references": evidence_ids,
                    }
                ],
            }
        )
        try:
            with self.session.begin_nested():
                result = ComponentService(self.session).record(component_request)
                if result.status == ResultStatus.POSTED:
                    self._assert_round6_final_dependency_constraints_now()
                if result.status == ResultStatus.NEEDS_INFORMATION:
                    missing = result.missing_information
                    if missing == ["evidence_references"]:
                        missing = [
                            {
                                "field": "evidence_references",
                                "reason": "正式工资或年终奖入账需要预览时登记的原始依据",
                            }
                        ]
                    return PayrollResult(
                        status=PayrollResultStatus.NEEDS_INFORMATION,
                        batch_id=batch.id,
                        calculation_hash=batch.calculation_hash,
                        missing_information=missing,
                    )
                if result.status != ResultStatus.POSTED:
                    errors = [
                        "PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"
                        if code == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
                        else code
                        for code in result.errors
                    ]
                    return PayrollResult(status=PayrollResultStatus.REJECTED, errors=errors)
                self.session.refresh(batch)
                return self._payroll_result_for_batch(
                    batch,
                    idempotent_replay=bool(result.data.get("idempotent_replay")),
                )
        except AccountingPeriodError as exc:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED,
                errors=[exc.code],
            )
        except IntegrityError:
            return PayrollResult(
                status=PayrollResultStatus.REJECTED, errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"]
            )
        except ValueError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[str(exc)])
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

    def compile_payroll_accrual_component(
        self,
        component,
        *,
        planned_regular_plans: dict[str, ComponentPostingPlan | None] | None = None,
    ) -> tuple[ComponentPostingPlan, list[uuid.UUID]]:
        """Validate one calculated payroll snapshot and return its formal component plan."""

        from .component_service import MissingFacts

        batch = self.session.scalar(
            select(PayrollBatch)
            .where(
                PayrollBatch.id == component.batch_id,
                PayrollBatch.org_id == self._component_org_id,
            )
            .with_for_update()
        )
        if batch is None:
            raise ValueError("PAYROLL_BATCH_NOT_FOUND")
        if batch.status != "calculated" or batch.business_event_id is not None:
            raise ValueError("PAYROLL_BATCH_IS_NOT_CONFIRMABLE")
        planned_regular_plans = planned_regular_plans or {}
        local_regular_batches: dict[uuid.UUID, tuple[str, PayrollBatch, ComponentPostingPlan]] = {}
        if planned_regular_plans:
            if (
                batch.batch_kind != PayrollBatchKind.ANNUAL_BONUS.value
                or batch.tax_method != AnnualBonusTaxMethod.COMBINED.value
            ):
                raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_REQUIRES_COMBINED_BONUS")
            for local_regular_key, planned_regular_plan in planned_regular_plans.items():
                if planned_regular_plan is None or planned_regular_plan.kind != "payroll_accrual":
                    raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_INVALID")
                planned_batch_id = planned_regular_plan.derived.get("payroll_batch_id")
                planned_hash = planned_regular_plan.derived.get("calculation_hash")
                if planned_batch_id is None or planned_hash is None:
                    raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_INVALID")
                local_regular_batch = self.session.scalar(
                    select(PayrollBatch).where(
                        PayrollBatch.org_id == batch.org_id,
                        PayrollBatch.id == uuid.UUID(str(planned_batch_id)),
                    )
                )
                if (
                    local_regular_batch is None
                    or local_regular_batch.batch_kind != PayrollBatchKind.REGULAR.value
                    or local_regular_batch.status != "calculated"
                    or local_regular_batch.business_event_id is not None
                    or local_regular_batch.calculation_hash != planned_hash
                    or local_regular_batch.id in local_regular_batches
                ):
                    raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_INVALID")
                local_regular_batches[local_regular_batch.id] = (
                    local_regular_key,
                    local_regular_batch,
                    planned_regular_plan,
                )
        if batch.posting_date != self._component_posting_date:
            raise ValueError("PAYROLL_COMPONENT_POSTING_DATE_MISMATCH")
        if component.business_date != batch.posting_date:
            raise ValueError("PAYROLL_COMPONENT_BUSINESS_DATE_MISMATCH")
        if batch.calculation_hash != component.calculation_hash:
            raise ValueError("STALE_PAYROLL_CALCULATION")
        if batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value and batch.tax_method is None:
            raise ValueError("ANNUAL_BONUS_TAX_METHOD_REQUIRED")
        evidence_ids = list(
            self.session.scalars(
                select(PayrollBatchEvidence.evidence_id)
                .where(
                    PayrollBatchEvidence.org_id == batch.org_id,
                    PayrollBatchEvidence.payroll_batch_id == batch.id,
                )
                .order_by(PayrollBatchEvidence.evidence_id)
            )
        )
        requested_evidence_ids = [
            uuid.UUID(value)
            for value in batch.calculation_input.get("request", {}).get("evidence_references", [])
        ]
        if sorted(evidence_ids) != sorted(requested_evidence_ids):
            raise ValueError("STALE_PAYROLL_CALCULATION")
        if not evidence_ids:
            raise MissingFacts([f"components.{component.key}.evidence_references"])
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
        lines = list(
            self.session.scalars(
                select(PayrollLine)
                .where(PayrollLine.payroll_batch_id == batch.id)
                .order_by(PayrollLine.id)
            )
        )
        if not lines:
            raise ValueError("STALE_PAYROLL_CALCULATION")
        local_regular_proofs = []
        if local_regular_batches:
            snapshots = batch.calculation_input.get("employee_snapshots", [])
            snapshot_by_employee = {
                uuid.UUID(snapshot["employee_id"]): snapshot
                for snapshot in snapshots
                if snapshot.get("employee_id")
            }
            parent_lines_by_batch: dict[uuid.UUID, dict[uuid.UUID, PayrollLine]] = {}
            used_lines_by_batch: dict[uuid.UUID, list[str]] = {
                batch_id: [] for batch_id in local_regular_batches
            }
            for batch_id in local_regular_batches:
                parent_lines = list(
                    self.session.scalars(
                        select(PayrollLine)
                        .where(PayrollLine.payroll_batch_id == batch_id)
                        .order_by(PayrollLine.id)
                    )
                )
                by_employee = {line.employee_id: line for line in parent_lines}
                if len(by_employee) != len(parent_lines):
                    raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_INVALID")
                parent_lines_by_batch[batch_id] = by_employee
            for line in lines:
                source_batch_id = line.regular_payroll_batch_id
                if source_batch_id not in local_regular_batches:
                    continue
                _, local_regular_batch, _ = local_regular_batches[source_batch_id]
                parent_line = parent_lines_by_batch[source_batch_id].get(line.employee_id)
                snapshot = snapshot_by_employee.get(line.employee_id)
                if (
                    parent_line is None
                    or snapshot is None
                    or snapshot.get("regular_payroll_batch_id") != str(source_batch_id)
                    or snapshot.get("regular_payroll_line_id") != str(parent_line.id)
                    or snapshot.get("regular_payroll_calculation_hash")
                    != local_regular_batch.calculation_hash
                ):
                    raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_INVALID")
                used_lines_by_batch[source_batch_id].append(str(parent_line.id))
            if any(not line_ids for line_ids in used_lines_by_batch.values()):
                raise ValueError("PAYROLL_LOCAL_REGULAR_PARENT_UNUSED")
            local_regular_proofs = [
                {
                    "component_key": local_regular_batches[batch_id][0],
                    "batch_id": str(batch_id),
                    "calculation_hash": local_regular_batches[batch_id][1].calculation_hash,
                    "employee_line_ids": sorted(line_ids),
                }
                for batch_id, line_ids in used_lines_by_batch.items()
            ]
            local_regular_proofs.sort(key=lambda value: value["component_key"])
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
                raise ValueError(exc.code) from exc
        if not self.session.info.get("event_amendment"):
            try:
                stored_request = PreviewPayrollRequest.model_validate(
                    batch.calculation_input["request"]
                )
                recalculated = self._calculate_payroll(
                    stored_request,
                    allowed_calculated_regular_batch_ids=set(local_regular_batches),
                    require_calculated_regular_slot=bool(local_regular_batches),
                )
            except (KeyError, NeedsInformationError, CalculationValidationError, ValueError) as exc:
                raise ValueError("STALE_PAYROLL_CALCULATION") from exc
            if (
                recalculated["missing"]
                or recalculated["calculation_hash"] != batch.calculation_hash
                or recalculated["policy_snapshot"] != batch.policy_snapshot
                or set(recalculated["actual_item_ids"]) != stored_actual_item_ids
                or set(recalculated["first_wage_treatment_ids"]) != stored_first_wage_treatment_ids
            ):
                raise ValueError("STALE_PAYROLL_CALCULATION")
        try:
            self._reserve_payroll_tax_state_slots(batch, lines)
        except CalculationValidationError as exc:
            raise ValueError(exc.code) from exc
        self._create_payroll_withholding_entitlements(batch, lines)
        self.session.flush()
        entries, raw_open_items = self._payroll_accrual_template(batch, lines)
        open_items = self._name_payroll_obligations(raw_open_items)
        entitlements = list(
            self.session.scalars(
                select(PayrollWithholdingEntitlement)
                .where(
                    PayrollWithholdingEntitlement.org_id == batch.org_id,
                    PayrollWithholdingEntitlement.payroll_line_id.in_([line.id for line in lines]),
                )
                .order_by(
                    PayrollWithholdingEntitlement.payroll_line_id,
                    PayrollWithholdingEntitlement.contribution_group,
                    PayrollWithholdingEntitlement.insurance_kind,
                )
            )
        )
        entitlement_by_line: dict[uuid.UUID, list[PayrollWithholdingEntitlement]] = {}
        for entitlement in entitlements:
            entitlement_by_line.setdefault(entitlement.payroll_line_id, []).append(entitlement)
        employee_by_id = {
            employee.id: employee
            for employee in self.session.scalars(
                select(Employee).where(Employee.id.in_([line.employee_id for line in lines]))
            )
        }
        profile_by_id = {
            profile.id: profile
            for profile in self.session.scalars(
                select(EmployeePayrollProfileVersion).where(
                    EmployeePayrollProfileVersion.id.in_(
                        [line.employee_payroll_profile_version_id for line in lines]
                    )
                )
            )
        }
        derived = {
            "payroll_batch_id": str(batch.id),
            "calculation_hash": batch.calculation_hash,
            "salary_sources": [
                {
                    "open_item_key": f"salary:{line.id}",
                    "payroll_line_id": str(line.id),
                    "employee_id": str(line.employee_id),
                    "counterparty_id": str(employee_by_id[line.employee_id].counterparty_id),
                    "gross_salary_fen": line.gross_salary_fen,
                    "expense_role": profile_by_id[
                        line.employee_payroll_profile_version_id
                    ].expense_role,
                    "entitlements": [
                        {
                            "id": str(item.id),
                            "contribution_group": item.contribution_group,
                            "insurance_kind": item.insurance_kind,
                            "amount_fen": item.amount_fen,
                        }
                        for item in entitlement_by_line.get(line.id, [])
                    ],
                }
                for line in lines
                if line.gross_salary_fen
            ],
        }
        if local_regular_proofs:
            derived["local_regular_payroll_proofs"] = local_regular_proofs

        def apply(session, event, persisted):
            session.add(
                PayrollEventLink(
                    org_id=batch.org_id,
                    event_id=event.id,
                    component_id=persisted.id,
                    payroll_batch_id=batch.id,
                    link_kind="payroll_accrual",
                )
            )
            batch.status = "posted"
            batch.business_event_id = event.id
            batch.confirmed_by = None
            batch.confirmation_note = None
            batch.confirmed_at = datetime.now(UTC)
            if (
                batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value
                and batch.tax_method == "separate"
            ):
                for line in lines:
                    session.add(
                        AnnualBonusUsage(
                            org_id=batch.org_id,
                            employee_id=line.employee_id,
                            tax_year=self._batch_tax_period(batch).year,
                            payroll_batch_id=batch.id,
                            payroll_line_id=line.id,
                        )
                    )
            session.add(
                AuditLog(
                    org_id=batch.org_id,
                    event_id=event.id,
                    action="payroll_confirmed",
                    actor="ai_agent:ai-accounting-core",
                    details={
                        "batch_id": str(batch.id),
                        "calculation_hash": batch.calculation_hash,
                    },
                )
            )

        return (
            ComponentPostingPlan(
                key=component.key,
                kind=component.kind,
                facts=component.model_dump(mode="json"),
                derived=derived,
                entries=entries,
                open_items=open_items,
                rule_version=batch.policy_snapshot.get("version"),
                effects=[apply],
            ),
            evidence_ids,
        )

    @property
    def _component_org_id(self) -> uuid.UUID:
        return self._active_component_request.org_id

    @property
    def _component_posting_date(self) -> date:
        return self._active_component_request.posting_date

    def _confirm_payroll_write(self, request: ConfirmPayrollRequest) -> PayrollResult:
        """Compatibility shim for internal callers; all formal writes use components."""

        return self.confirm_payroll(request)

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
        payment_ids = set(
            self.session.scalars(
                select(PayrollEventLink.event_id).where(
                    PayrollEventLink.org_id == org_id,
                    PayrollEventLink.payroll_batch_id == batch.id,
                    PayrollEventLink.link_kind.in_(("salary_payment", "statutory_payment")),
                )
            )
        )
        payment_events = [event for event in events if event.id in payment_ids]
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
                    "counterparty_id": str(item.counterparty_id) if item.counterparty_id else None,
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
                "LATER_PAYROLL_TAX_STATE_EXISTS",
                "later posted tax state must be included in the correction scope",
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
                    existing_slot = self.session.scalar(
                        select(PayrollTaxStateSlot).where(
                            PayrollTaxStateSlot.org_id == batch.org_id,
                            PayrollTaxStateSlot.employee_id == line.employee_id,
                            PayrollTaxStateSlot.tax_year == year,
                            PayrollTaxStateSlot.tax_month == month,
                        )
                    )
                    preserved = self.session.info.get("preserve_accrual_batch_ids", {})
                    if (
                        batch.id in preserved.get("payroll", set())
                        and existing_slot is not None
                        and existing_slot.regular_batch_id == batch.id
                        and existing_slot.final_batch_id == batch.id
                    ):
                        continue
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

    def _calculate_payroll(
        self,
        request: PreviewPayrollRequest,
        *,
        allow_calculated_regular: bool = False,
        allowed_calculated_regular_batch_ids: set[uuid.UUID] | None = None,
        require_calculated_regular_slot: bool = False,
    ) -> dict[str, Any]:
        period = YearMonth(int(request.payroll_period[:4]), int(request.payroll_period[5:]))
        payment_date = request.payment_date
        if request.batch_kind == PayrollBatchKind.ANNUAL_BONUS and payment_date is None:
            raise CalculationValidationError(
                "PAYROLL_PAYMENT_DATE_REQUIRED",
                "annual bonus payroll requires its actual payment date",
            )
        tax_period = (
            period
            if request.batch_kind == PayrollBatchKind.REGULAR
            else YearMonth(payment_date.year, payment_date.month)
        )
        tax_policy_date = (
            period.end_date if request.batch_kind == PayrollBatchKind.REGULAR else payment_date
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
                bonus_policy.assert_effective(payment_date)
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
        snapshot_parameters = {
            "contribution_rules": deepcopy(contribution_parameters.get("contribution_rules", [])),
            "employee_contribution_shortfall_treatment": contribution_parameters.get(
                "employee_contribution_shortfall_treatment", "reject"
            ),
            "income_tax": deepcopy(tax_parameters["income_tax"]),
            "annual_bonus": deepcopy(tax_parameters.get("annual_bonus")),
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
                or item.wage_tax_scope == PayrollWageTaxScope.WAGE_INCOME
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
                    item.wage_tax_scope == PayrollWageTaxScope.WAGE_INCOME
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
                if item.wage_tax_scope == PayrollWageTaxScope.CONTRIBUTIONS_ONLY:
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
                            "wage_tax_scope": "contributions_only",
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
                        "wage_tax_scope": "wage_income",
                        "accounting_gross_salary_fen": payroll_input.gross_salary_fen,
                        "tax_reported_salary_fen": payroll_input.tax_reported_salary_fen,
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
                                "standard_deduction_start_month": (standard_deduction_start_month),
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
                                payment_date=payment_date,
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
                    allow_calculated=(
                        allow_calculated_regular
                        or item.regular_payroll_batch_id
                        in (allowed_calculated_regular_batch_ids or set())
                    ),
                    require_calculated_slot=require_calculated_regular_slot,
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
                            income_date=payment_date,
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
                            payment_date=payment_date,
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
                                income_date=payment_date,
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
                        "regular_payroll_calculation_hash": regular_batch.calculation_hash,
                        "regular_payroll_line_id": str(regular_line.id),
                        "regular_tax_input": self._tax_input_dict(regular_tax_input),
                    }
                )
        if missing:
            return {"missing": missing, "scenarios": scenarios}
        calculation_input = {
            "request": request.model_dump(
                mode="json",
                exclude={
                    "description": True,
                    "employee_items": {"__all__": {"tax_reporting_difference_reason"}},
                },
            ),
            "employee_snapshots": input_snapshots,
        }
        accounting_lines = [
            {
                key: value
                for key, value in prepared.items()
                if key != "tax_reporting_difference_reason"
            }
            for prepared in prepared_lines
        ]
        hash_payload = {
            "calculation_input": calculation_input,
            "policy_snapshot": policy_snapshot,
            "lines": accounting_lines,
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
                "tax_timing_date": (
                    payment_date.isoformat()
                    if request.batch_kind == PayrollBatchKind.ANNUAL_BONUS
                    else period.end_date.isoformat()
                ),
                "cash_settlement_tracking": "later_bank_statement_and_payment_event",
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
        *,
        allow_calculated: bool = False,
        require_calculated_slot: bool = False,
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
        if batch.batch_kind != PayrollBatchKind.REGULAR.value:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "combined annual bonus requires a regular payroll batch",
            )
        calculated_source = batch.status == "calculated" and allow_calculated
        if batch.status != "posted" and not calculated_source:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "combined annual bonus requires a posted regular payroll batch or its explicit "
                "local planned parent",
            )
        if calculated_source and batch.business_event_id is not None:
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "calculated regular payroll cannot already belong to a formal event",
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
        if slot is None and (not calculated_source or require_calculated_slot):
            raise CalculationValidationError(
                "INVALID_REGULAR_PAYROLL_DEPENDENCY",
                "referenced regular payroll has no formal tax-state slot",
            )
        if slot is not None and slot.final_batch_id != batch.id:
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
        if batch.payment_date is None:
            raise CalculationValidationError(
                "PAYROLL_PAYMENT_DATE_REQUIRED",
                "annual bonus payroll requires its actual payment date",
            )
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
                },
            },
            {"step": "tax_state_after", "values": self._tax_state_dict(tax_state)},
        ]
        return {
            "employee_id": employee.id,
            "employee_payroll_profile_version_id": profile.id,
            "wage_tax_scope": "wage_income",
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
            "wage_tax_scope": "contributions_only",
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
            "wage_tax_scope": "not_applicable",
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
                        # A regular payroll accrual does not predict its later
                        # bank settlement date.  The payable remains open until
                        # a typed payment event matches the real bank entry.
                        due_date=(
                            batch.payment_date
                            if batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value
                            else None
                        ),
                        payable_category="salary",
                        key=f"salary:{line.id}",
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
        for (category, insurance_kind), amount in statutory_amounts.items():
            plans.append(
                OpenItemPlan(
                    counterparty_id=None,
                    item_type="payable",
                    original_amount_fen=amount,
                    due_date=None,
                    payable_category=category,
                    insurance_kind=insurance_kind,
                )
            )
        return entries, plans

    @staticmethod
    def _name_payroll_obligations(plans: list[OpenItemPlan]) -> list[OpenItemPlan]:
        roles = {
            "salary": "employee_salary_payable",
            "employer_social": "employer_social_payable",
            "employer_housing": "employer_housing_fund_payable",
            "withheld_employee_social": "withheld_employee_social_payable",
            "withheld_employee_housing": "withheld_employee_housing_fund_payable",
            "individual_income_tax": "individual_income_tax_payable",
        }
        return [
            replace(
                plan,
                key=(
                    plan.key
                    if plan.key != "primary"
                    else (
                        f"{plan.payable_category or plan.item_type}:{plan.counterparty_id}:"
                        f"{plan.insurance_kind or 'total'}"
                    )
                ),
                account_role=roles[plan.payable_category]
                if plan.item_type == "payable"
                else "employee_receivable",
            )
            for plan in plans
        ]

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
            "wage_tax_scope": line.wage_tax_scope,
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
            if not self._tax_period_event_matches_request(existing, request):
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
                if not self._tax_period_event_matches_request(existing, request):
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
            if not self._tax_period_event_matches_request(existing_event, request):
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
            try:
                entries.extend(vat_relief_entries(self.session, request.org_id, tax_result))
            except ValueError as exc:
                code = str(exc)
                if code.startswith("TAX_"):
                    return FinanceResult(status=ResultStatus.REJECTED, errors=[code])
                raise
            entries.append(
                Entry(account_role="tax_relief_income", credit_fen=tax_result.vat_relief_fen)
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
        source_event_ids = [uuid.UUID(source["event_id"]) for source in tax_result.source_events]
        source_evidence = list(
            self.session.scalars(
                select(event_evidence.c.evidence_id)
                .where(
                    event_evidence.c.org_id == request.org_id,
                    event_evidence.c.event_id.in_(source_event_ids),
                )
                .distinct()
            )
        )
        from .component_service import ComponentService

        result = ComponentService(self.session).record(
            RecordEventRequest.model_validate(
                {
                    "org_id": request.org_id,
                    "idempotency_key": request.idempotency_key,
                    "posting_date": request.adjustment_posting_date,
                    "description": (f"税务期间结算 {request.start_date} 至 {request.end_date}"),
                    "evidence_references": source_evidence,
                    "components": [
                        {
                            "key": "tax_period",
                            "kind": "tax_relief",
                            "business_date": request.end_date,
                            "start_date": request.start_date,
                            "end_date": request.end_date,
                            "calculation_hash": request.calculation_hash,
                        }
                    ],
                }
            )
        )
        if result.status != ResultStatus.POSTED:
            return result
        period_record = self.session.scalar(
            select(TaxPeriod).where(
                TaxPeriod.org_id == request.org_id,
                TaxPeriod.adjustment_event_id == result.event_id,
            )
        )
        if period_record is None:
            raise ValueError("TAX_PERIOD_COMPONENT_RESULT_MISSING")
        self._assert_tax_period_range_constraint_now()
        result.rule_version = tax_result.rule_version
        result.trace = tax_result.trace
        result.data = {
            "tax_period_id": str(period_record.id),
            "calculation_hash": tax_result.calculation_hash,
            "idempotent_replay": False,
        }
        return result

    def _tax_period_event_matches_request(
        self, event: BusinessEvent, request: TaxPeriodConfirmRequest
    ) -> bool:
        component = self.session.scalar(
            select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == event.id,
                BusinessEventComponent.kind == "tax_relief",
            )
        )
        return bool(
            component is not None
            and event.posting_date == request.adjustment_posting_date
            and component.facts.get("start_date") == request.start_date.isoformat()
            and component.facts.get("end_date") == request.end_date.isoformat()
            and component.facts.get("calculation_hash") == request.calculation_hash
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
                wage_tax_scope=source.wage_tax_scope,
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

        request_payload_hash = self._request_payload_hash(request)
        try:
            with self.session.begin_nested():
                return FinanceService._reverse_event_write(self, request)
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
        except ValueError as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[str(exc)])
        except OperationalError:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["PAYROLL_CONCURRENT_WRITE_CONFLICT"],
            )

    def _payroll_tax_dependent_batch_ids(
        self,
        batch: PayrollBatch,
        lines: list[PayrollLine],
        *,
        excluded_batch_ids: set[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Find actual external cumulative-tax consumers for any whole-event correction."""
        employee_ids = {
            line.employee_id for line in lines if self._line_uses_cumulative_tax_state(batch, line)
        }
        if not employee_ids:
            return set()
        period = self._batch_tax_period(batch)
        candidates = self.session.execute(
            select(PayrollBatch, PayrollLine)
            .join(PayrollLine, PayrollLine.payroll_batch_id == PayrollBatch.id)
            .where(
                PayrollBatch.org_id == batch.org_id,
                PayrollBatch.status == "posted",
                PayrollBatch.reversal_of_batch_id.is_(None),
                PayrollBatch.id.not_in(excluded_batch_ids),
                PayrollLine.employee_id.in_(employee_ids),
            )
        ).all()
        return {
            candidate.id
            for candidate, line in candidates
            if self._line_uses_cumulative_tax_state(candidate, line)
            and (
                (
                    self._batch_tax_period(candidate).year == period.year
                    and self._batch_tax_period(candidate) > period
                )
                or line.regular_payroll_batch_id == batch.id
            )
        }

    def _reverse_event_write(self, request: ReverseEventRequest) -> FinanceResult:
        lock_income_tax(self.session, request.org_id)
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
        from .corrections import correction_route

        route = correction_route(self.session, request.org_id, original.id)
        if route["route"] != "linked_reversal":
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["OPEN_PERIOD_REQUIRES_AMENDMENT"],
                data={**route, "next_action": "finance_preview_correction"},
            )
        if error := EnterpriseIncomeTaxService(self.session).reversal_error(original):
            return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
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
                BusinessEvent.id != original.id,
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
                TaxPeriod.adjustment_event_id != original.id,
            )
        )
        if locked_tax_source is not None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["TAX_PERIOD_SOURCE_LOCKED"],
            )
        from .component_lifecycle import (
            domain_dependency_error,
            reversal_plans,
            reverse_domain_allocations,
        )

        if error := domain_dependency_error(self.session, original):
            return FinanceResult(status=ResultStatus.REJECTED, errors=[error])
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
        external_settlement = self.session.scalar(
            select(Settlement.id)
            .where(
                Settlement.open_item_id.in_([item.id for item in source_items]),
                Settlement.payment_event_id != original.id,
                Settlement.reversed.is_(False),
            )
            .limit(1)
        )
        if external_settlement is not None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["REVERSE_SETTLEMENT_EVENTS_BEFORE_SOURCE_EVENT"],
            )
        payroll_batches = list(
            self.session.scalars(
                select(PayrollBatch)
                .join(
                    PayrollEventLink,
                    (PayrollEventLink.org_id == PayrollBatch.org_id)
                    & (PayrollEventLink.payroll_batch_id == PayrollBatch.id),
                )
                .where(
                    PayrollEventLink.org_id == request.org_id,
                    PayrollEventLink.event_id == original.id,
                    PayrollEventLink.link_kind == "payroll_accrual",
                )
                .order_by(PayrollBatch.id)
                .with_for_update()
            )
        )
        owned_payroll_batch_ids = {batch.id for batch in payroll_batches}
        labor_batches = list(
            self.session.scalars(
                select(LaborRemunerationBatch)
                .join(
                    LaborRemunerationEventLink,
                    (LaborRemunerationEventLink.org_id == LaborRemunerationBatch.org_id)
                    & (LaborRemunerationEventLink.batch_id == LaborRemunerationBatch.id),
                )
                .where(
                    LaborRemunerationEventLink.org_id == request.org_id,
                    LaborRemunerationEventLink.event_id == original.id,
                    LaborRemunerationEventLink.link_kind == "accrual",
                )
                .with_for_update()
            )
        )
        for payroll_batch in payroll_batches:
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
                    PayrollContributionSupplement.event_id != original.id,
                    BusinessEvent.status == "posted",
                )
            )
            if active_supplement is not None:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["REVERSE_DEPENDENT_EVENTS_FIRST"],
                )
            payroll_lines = self.session.scalars(
                select(PayrollLine).where(PayrollLine.payroll_batch_id == payroll_batch.id)
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
                dependent = bool(
                    self._payroll_tax_dependent_batch_ids(
                        payroll_batch,
                        payroll_lines,
                        excluded_batch_ids=owned_payroll_batch_ids,
                    )
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
                and slot.final_batch_id not in owned_payroll_batch_ids
                for slot in slots
            ):
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["REVERSE_DEPENDENT_PAYROLL_BATCHES_FIRST"],
                )

        reversal = build_business_event(
            self.session,
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_payload_hash,
            event_type="payroll_accrual" if payroll_batches else "reversal",
            status="draft",
            description=f"冲正 {original.id}: {request.reason}",
            facts={
                "original_event_id": str(original.id),
                "reversal": True,
                "payroll_batch_ids": [str(batch.id) for batch in payroll_batches],
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

        plans = reversal_plans(self.session, original, original_voucher)
        reversal_payroll_batches = []
        # Release dependent bonus slots before removing their regular source;
        # the original row-lock acquisition above remains in stable UUID order.
        for payroll_batch in sorted(
            payroll_batches,
            key=lambda batch: (
                batch.batch_kind == PayrollBatchKind.REGULAR.value,
                batch.id,
            ),
        ):
            reversal_payroll_batch = self._create_payroll_reversal_batch(
                payroll_batch, reversal, request
            )
            reversal_payroll_batches.append(reversal_payroll_batch)
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
        reversal.facts["payroll_reversal_batch_ids"] = [
            str(batch.id) for batch in reversal_payroll_batches
        ]
        # The reversal edge belongs to the still-draft reversal event.  Flush
        # it before promoting that event (and its payroll reversal batch) to
        # their immutable final states.
        self.session.flush()
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
        for item in source_items:
            item.status = "reversed"
        reverse_domain_allocations(self.session, original, reversal)

        tax_periods = self.session.scalars(
            select(TaxPeriod).where(TaxPeriod.adjustment_event_id == original.id)
        ).all()
        for tax_period in tax_periods:
            tax_period.status = "reversed"
        if tax_periods:
            self.session.flush(tax_periods)
        original.status = "reversed"
        original.reversed_by_event_id = reversal.id
        for payroll_batch in payroll_batches:
            payroll_batch.status = "reversed"
            if payroll_batch.batch_kind == PayrollBatchKind.ANNUAL_BONUS.value:
                usages = self.session.scalars(
                    select(AnnualBonusUsage).where(
                        AnnualBonusUsage.payroll_batch_id == payroll_batch.id
                    )
                ).all()
                for usage in usages:
                    self.session.delete(usage)
        for labor_batch in labor_batches:
            labor_batch.status = "reversed"
        for reversal_payroll_batch in reversal_payroll_batches:
            reversal_payroll_batch.status = "posted"
        voucher = commit_posting_plan(
            self.session,
            event=reversal,
            components=plans,
            posting_date=request.posting_date,
            description=reversal.description,
            reversal_of=original_voucher,
        )
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=reversal.id,
                action="event_reversed",
                details={
                    "original_event_id": str(original.id),
                    "reason": request.reason,
                    "payroll_reversal_batch_ids": [
                        str(batch.id) for batch in reversal_payroll_batches
                    ],
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
