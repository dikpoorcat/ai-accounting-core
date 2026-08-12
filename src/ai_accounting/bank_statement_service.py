"""Database-backed formal bank-statement workflows.

Only controlled CSV files are exposed by the formal service.  Parsing and
calculation stay pure; this layer resolves every organization, account,
period, close, duplicate, and evidence fact from the database.
"""

from __future__ import annotations

import json
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import null, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from .accounting_periods import china_current_date
from .bank_statement_schemas import (
    BankReconciliationEvidenceFact,
    BankReconciliationImportActionFact,
    BankReconciliationImportedTransactionFact,
    BankReconciliationPreview,
    BankReconciliationScopePreview,
    BankReconciliationSystemFacts,
    BankStatementActionResult,
    BankStatementActionStatus,
    BankStatementImportEvidenceFact,
    BankStatementImportPreview,
    BankStatementImportRowSystemFact,
    BankStatementImportSystemFacts,
    BankStatementInformationRequirement,
    BankStatementIssue,
    BankStatementPeriodProjection,
    BankStatementPreviewStatus,
    BankStatementTransactionSnapshot,
    ConfirmBankReconciliationRequest,
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    ConfirmLateBankEvidenceRequest,
    GetBankStatementActivityRequest,
    LateBankEvidencePreview,
    ParsedBankStatement,
    PreviewBankReconciliationRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
    PreviewBankStatementImportRequest,
    PreviewLateBankEvidenceRequest,
)
from .bank_statements import (
    calculate_bank_reconciliation,
    canonical_json,
    canonical_sha256,
    parse_bank_statement_bytes,
    preview_bank_statement_import,
)
from .config import Settings, get_settings
from .models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    Account,
    AccountBankReconciliationScopeHistory,
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseSource,
    BankReconciliation,
    BankReconciliationAction,
    BankReconciliationEvidence,
    BankReconciliationFailure,
    BankReconciliationImportAction,
    BankReconciliationScopeAction,
    BankReconciliationScopeActionEvidence,
    BankReconciliationTransaction,
    BankStatementImportAction,
    BankStatementImportActionEvidence,
    BankStatementImportFailure,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    LateBankEvidenceAction,
    LateBankEvidenceActionEvidence,
    Organization,
    Voucher,
    VoucherLine,
)
from .path_security import PathSecurityError, read_regular_file_in_root


class _BankDecision(ValueError):
    def __init__(self, code: str, *, field_path: str | None = None) -> None:
        self.code = code
        self.field_path = field_path
        super().__init__(code)


class BankStatementService:
    """Resolve formal import previews without accepting caller-supplied state."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        current_date: date | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._current_date = current_date

    def preview_bank_statement_import(
        self,
        request: PreviewBankStatementFileImportRequest,
    ) -> BankStatementImportPreview:
        """Read one allowlisted CSV once and apply authoritative database facts."""

        calculation_request = request.calculation_request()
        precondition = self._import_precondition(calculation_request)
        if precondition is not None:
            return precondition
        try:
            statement_bytes = self._read_controlled_csv(request.source_file_name)
        except PathSecurityError as exc:
            return self._rejected_import_preview(
                self._safe_path_error_code(exc),
                field_path="source_file_name",
            )
        parsed = parse_bank_statement_bytes(calculation_request, statement_bytes)
        if parsed.status == "rejected":
            return BankStatementImportPreview(
                status=BankStatementPreviewStatus.REJECTED,
                source_sha256=parsed.source_sha256,
                errors=parsed.errors,
                trace=[{"stage": "statement_parse_rejected"}],
            )
        account = self._bank_account(request.org_id, request.bank_account_code)
        assert account is not None  # guarded by _import_precondition
        scope_issue = self._validate_account_dates(account, parsed)
        if scope_issue is not None:
            return BankStatementImportPreview(
                status=BankStatementPreviewStatus.NEEDS_INFORMATION,
                source_sha256=parsed.source_sha256,
                missing_information=[scope_issue],
                trace=[{"stage": "bank_account_scope_needs_information"}],
            )
        facts = self._import_system_facts(calculation_request, parsed)
        return preview_bank_statement_import(calculation_request, parsed, facts)

    def confirm_bank_statement_import(
        self,
        request: ConfirmBankStatementFileImportRequest,
    ) -> BankStatementActionResult:
        """Lock, reparse the same bytes, re-calculate, and append one import action."""

        calculation_request = request.calculation_request()
        precondition = self._import_precondition(calculation_request)
        if precondition is not None:
            return self._action_from_preview(precondition)
        attribution_id = self.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
        if not isinstance(attribution_id, uuid.UUID):
            return self._action_error("BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED")
        self._lock_tax_period_org(request.org_id)
        locked_precondition = self._import_precondition(calculation_request, lock=True)
        if locked_precondition is not None:
            return self._action_from_preview(locked_precondition)
        try:
            statement_bytes = self._read_controlled_csv(request.source_file_name)
        except PathSecurityError as exc:
            code = self._safe_path_error_code(exc)
            request_payload_hash = canonical_sha256(
                {
                    "command": "finance_confirm_bank_statement_import",
                    "request": request.model_dump(
                        mode="json",
                        exclude={"source_file_name"},
                    ),
                    "source_sha256": None,
                    "input_failure_code": code,
                }
            )
            return self._persist_import_unavailable_rejection(
                request,
                request_payload_hash,
                BankStatementIssue(code=code, field_path="source_file_name"),
                attribution_id,
            )
        discovery = parse_bank_statement_bytes(calculation_request, statement_bytes)
        for month in sorted({row.booking_date.replace(day=1) for row in discovery.rows}):
            self._lock_month(request.org_id, month)
        # Reparse the already-loaded immutable bytes after all relevant month
        # locks.  The file is never opened a second time in this confirmation.
        parsed = parse_bank_statement_bytes(calculation_request, statement_bytes)
        account = self._bank_account(request.org_id, request.bank_account_code)
        assert account is not None
        scope_issue = self._validate_account_dates(account, parsed)
        if scope_issue is not None:
            preview = BankStatementImportPreview(
                status=BankStatementPreviewStatus.NEEDS_INFORMATION,
                source_sha256=parsed.source_sha256,
                missing_information=[scope_issue],
            )
        elif parsed.status == "rejected":
            preview = BankStatementImportPreview(
                status=BankStatementPreviewStatus.REJECTED,
                source_sha256=parsed.source_sha256,
                errors=parsed.errors,
            )
        else:
            facts = self._import_system_facts(
                calculation_request,
                parsed,
                lock_periods=True,
            )
            preview = preview_bank_statement_import(calculation_request, parsed, facts)
        request_payload_hash = canonical_sha256(
            {
                "command": "finance_confirm_bank_statement_import",
                "request": request.model_dump(
                    mode="json",
                    exclude={"source_file_name"},
                ),
                "source_sha256": parsed.source_sha256,
            }
        )
        existing = self.session.scalar(
            select(BankStatementImportAction)
            .where(
                BankStatementImportAction.org_id == request.org_id,
                BankStatementImportAction.idempotency_key == request.idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_payload_hash != request_payload_hash:
                return self._action_error("BANK_STATEMENT_IDEMPOTENCY_PAYLOAD_MISMATCH")
            return self._replay_import_action(existing)
        if (
            preview.status != BankStatementPreviewStatus.CALCULATED
            or preview.calculation_hash != request.calculation_hash
        ):
            issues = self._preview_issues(preview)
            if preview.status == BankStatementPreviewStatus.CALCULATED:
                issues = [BankStatementIssue(code="BANK_STATEMENT_CALCULATION_STALE")]
            try:
                with self.session.begin_nested():
                    result = self._persist_import_rejection(
                        request,
                        parsed,
                        request_payload_hash,
                        issues,
                        attribution_id,
                    )
                    self._assert_import_constraints_now()
                    return result
            except IntegrityError:
                return self._import_concurrency_result(
                    request.org_id,
                    request.idempotency_key,
                    request_payload_hash,
                )
            except DBAPIError as exc:
                return self._action_error(self._bank_database_error_code(exc))
        try:
            with self.session.begin_nested():
                result = self._persist_import_success(
                    request,
                    parsed,
                    preview,
                    request_payload_hash,
                    attribution_id,
                )
                self._assert_import_constraints_now()
                return result
        except IntegrityError:
            # Exact source-row/external-id and idempotency constraints are the
            # concurrency backstop.  Never leak SQL or parameters.
            return self._import_concurrency_result(
                request.org_id,
                request.idempotency_key,
                request_payload_hash,
            )
        except DBAPIError as exc:
            return self._action_error(self._bank_database_error_code(exc))

    def preview_bank_reconciliation_scope(
        self,
        request: PreviewBankReconciliationScopeRequest,
    ) -> BankReconciliationScopePreview:
        """Preview one explicit complete bank-account scope snapshot."""

        return self._bank_scope_preview(request, lock=False)

    def confirm_bank_reconciliation_scope(
        self,
        request: ConfirmBankReconciliationScopeRequest,
    ) -> BankStatementActionResult:
        """Append one attributed scope action and atomically apply its snapshot."""

        attribution_id = self.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
        if not isinstance(attribution_id, uuid.UUID):
            return self._action_error("BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED")
        request_payload_hash = canonical_sha256(
            {
                "command": "finance_confirm_bank_reconciliation_scope",
                "request": request.model_dump(mode="json"),
            }
        )
        existing = self.session.scalar(
            select(BankReconciliationScopeAction)
            .where(
                BankReconciliationScopeAction.org_id == request.org_id,
                BankReconciliationScopeAction.idempotency_key
                == request.idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_payload_hash != request_payload_hash:
                return self._action_error(
                    "BANK_RECONCILIATION_SCOPE_IDEMPOTENCY_PAYLOAD_MISMATCH"
                )
            return self._replay_scope_action(existing)
        self._lock_tax_period_org(request.org_id)
        preview_request = PreviewBankReconciliationScopeRequest.model_validate(
            request.model_dump(exclude={"calculation_hash", "idempotency_key"})
        )
        preview = self._bank_scope_preview(preview_request, lock=True)
        if (
            preview.status != BankStatementPreviewStatus.CALCULATED
            or preview.calculation_hash != request.calculation_hash
        ):
            issues = self._scope_issues(preview)
            if preview.status == BankStatementPreviewStatus.CALCULATED:
                issues = [
                    BankStatementIssue(code="BANK_RECONCILIATION_SCOPE_CALCULATION_STALE")
                ]
            return self._persist_scope_rejection(
                request,
                request_payload_hash,
                issues[0],
                attribution_id,
            )
        payload = preview.data.get("calculation_payload")
        if not isinstance(payload, dict) or preview.calculation_hash is None:
            return self._action_error("BANK_RECONCILIATION_SCOPE_SNAPSHOT_INVALID")
        normalized_payload = json.loads(canonical_json(payload))
        try:
            with self.session.begin_nested():
                action = BankReconciliationScopeAction(
                    org_id=request.org_id,
                    action_type=request.action_type,
                    previous_action_id=request.previous_action_id,
                    target_account_id=(
                        uuid.UUID(str(normalized_payload["target_account_id"]))
                        if normalized_payload["target_account_id"] is not None
                        else None
                    ),
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_payload_hash,
                    calculation_payload=canonical_json(normalized_payload),
                    calculation_hash=preview.calculation_hash,
                    scope_snapshot=normalized_payload["scope"],
                    status="posted",
                    explanation=request.explanation,
                    error_count=0,
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                self._apply_scope_snapshot(
                    action,
                    request,
                    normalized_payload,
                    attribution_id,
                )
                self.session.add_all(
                    [
                        BankReconciliationScopeActionEvidence(
                            org_id=request.org_id,
                            action_id=action.id,
                            evidence_id=uuid.UUID(str(item["evidence_id"])),
                            evidence_sha256_at_action=str(item["sha256"]),
                        )
                        for item in normalized_payload["evidence"]
                    ]
                )
                organization = self.session.scalar(
                    select(Organization)
                    .where(Organization.id == request.org_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                assert organization is not None
                organization.bank_reconciliation_scope_current_action_id = action.id
                organization.bank_reconciliation_scope_confirmed_at = self._database_clock()
                self.session.flush()
                self._assert_scope_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.POSTED,
                    action_id=action.id,
                    calculation_hash=action.calculation_hash,
                    trace=[*preview.trace, {"stage": "bank_scope_action_posted"}],
                    data={
                        "scope": normalized_payload["scope"],
                        "affected_closed_periods": normalized_payload[
                            "affected_closed_periods"
                        ],
                    },
                )
        except IntegrityError:
            return self._scope_concurrency_result(request, request_payload_hash)
        except DBAPIError as exc:
            return self._action_error(self._scope_database_error_code(exc))

    def preview_late_bank_evidence(
        self,
        request: PreviewLateBankEvidenceRequest,
    ) -> LateBankEvidencePreview:
        """Calculate one append-only handling action for a late bank fact."""

        return self._late_bank_preview(request, lock=False)

    def confirm_late_bank_evidence(
        self,
        request: ConfirmLateBankEvidenceRequest,
    ) -> BankStatementActionResult:
        attribution_id = self.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
        if not isinstance(attribution_id, uuid.UUID):
            return self._action_error("BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED")
        payload_hash = canonical_sha256(
            {
                "command": "finance_confirm_late_bank_evidence",
                "request": request.model_dump(mode="json"),
            }
        )
        existing = self.session.scalar(
            select(LateBankEvidenceAction)
            .where(
                LateBankEvidenceAction.org_id == request.org_id,
                LateBankEvidenceAction.idempotency_key == request.idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                return self._action_error("LATE_BANK_EVIDENCE_IDEMPOTENCY_PAYLOAD_MISMATCH")
            return self._replay_late_action(existing)
        self._lock_tax_period_org(request.org_id)
        preview_request = PreviewLateBankEvidenceRequest.model_validate(
            request.model_dump(exclude={"calculation_hash", "idempotency_key"})
        )
        preview = self._late_bank_preview(preview_request, lock=True)
        if (
            preview.status != BankStatementPreviewStatus.CALCULATED
            or preview.calculation_hash != request.calculation_hash
        ):
            code, field_path = self._late_failure(preview)
            if preview.status == BankStatementPreviewStatus.CALCULATED:
                code, field_path = "LATE_BANK_EVIDENCE_CALCULATION_STALE", None
            return self._persist_late_rejection(
                request,
                payload_hash,
                code,
                field_path,
                attribution_id,
            )
        payload = preview.data.get("calculation_payload")
        if not isinstance(payload, dict) or preview.calculation_hash is None:
            return self._action_error("LATE_BANK_EVIDENCE_SNAPSHOT_INVALID")
        try:
            with self.session.begin_nested():
                action = LateBankEvidenceAction(
                    org_id=request.org_id,
                    bank_transaction_id=request.bank_transaction_id,
                    action_type=request.action_type,
                    status="posted",
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=payload_hash,
                    calculation_payload=canonical_json(payload),
                    calculation_hash=preview.calculation_hash,
                    handling_period_id=request.handling_period_id,
                    original_close_id=uuid.UUID(str(payload["original_close_id"])),
                    original_close_hash=str(payload["original_close_hash"]),
                    target_event_id=request.target_event_id,
                    result_event_id=request.result_event_id,
                    result_voucher_id=request.result_voucher_id,
                    workflow_name=(
                        str(payload["workflow_name"])
                        if payload["workflow_name"] is not None
                        else None
                    ),
                    explanation=request.explanation,
                    error_count=0,
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                self.session.add_all(
                    [
                        LateBankEvidenceActionEvidence(
                            org_id=request.org_id,
                            action_id=action.id,
                            evidence_id=uuid.UUID(str(item["evidence_id"])),
                            evidence_sha256_at_action=str(item["sha256"]),
                        )
                        for item in payload["evidence"]
                    ]
                )
                self.session.flush()
                self._assert_late_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.POSTED,
                    action_id=action.id,
                    calculation_hash=action.calculation_hash,
                    trace=[
                        *preview.trace,
                        {"stage": "late_bank_evidence_action_posted"},
                    ],
                    data={
                        "bank_transaction_id": str(action.bank_transaction_id),
                        "action_type": action.action_type,
                        "handling_period_id": str(action.handling_period_id),
                    },
                )
        except IntegrityError:
            return self._late_concurrency_result(request, payload_hash)
        except DBAPIError as exc:
            return self._action_error(self._late_database_error_code(exc))

    def preview_bank_reconciliation(
        self,
        request: PreviewBankReconciliationRequest,
    ) -> BankReconciliationPreview:
        return self._bank_reconciliation_preview(request, lock=False)

    def get_bank_statement_activity(
        self,
        request: GetBankStatementActivityRequest,
    ) -> dict[str, Any]:
        """Return a bounded typed projection without raw source rows or paths."""

        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        handling_period = (
            self.session.scalar(
                select(AccountingPeriod).where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.id == request.handling_period_id,
                )
            )
            if request.handling_period_id is not None
            else None
        )
        if request.handling_period_id is not None and handling_period is None:
            return {
                "status": "rejected",
                "errors": ["BANK_STATEMENT_HANDLING_PERIOD_NOT_FOUND"],
            }
        transaction_query = select(BankTransaction).where(
            BankTransaction.org_id == request.org_id
        )
        if request.bank_transaction_id is not None:
            transaction_query = transaction_query.where(
                BankTransaction.id == request.bank_transaction_id
            )
        if request.bank_account_code is not None:
            transaction_query = transaction_query.where(
                BankTransaction.bank_account_code == request.bank_account_code
            )
        if request.original_period_id is not None:
            transaction_query = transaction_query.where(
                BankTransaction.original_period_id == request.original_period_id
            )
        transactions = self.session.scalars(
            transaction_query.order_by(
                BankTransaction.booking_date.desc(), BankTransaction.id
            ).limit(request.limit)
        ).all()
        if request.bank_transaction_id is not None and not transactions:
            return {
                "status": "rejected",
                "errors": ["BANK_STATEMENT_TRANSACTION_NOT_FOUND"],
            }
        if request.original_period_id is not None:
            original_period = self.session.scalar(
                select(AccountingPeriod.id).where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.id == request.original_period_id,
                )
            )
            if original_period is None:
                return {
                    "status": "rejected",
                    "errors": ["BANK_STATEMENT_ORIGINAL_PERIOD_NOT_FOUND"],
                }
        if handling_period is not None:
            transactions = [
                item
                for item in transactions
                if item.is_late
                and (
                    original := self.session.get(
                        AccountingPeriod, item.original_period_id
                    )
                )
                is not None
                and original.end_date < handling_period.start_date
            ]
        active_matches = self.session.scalars(
            select(BankTransactionMatch).where(
                BankTransactionMatch.org_id == request.org_id,
                BankTransactionMatch.bank_transaction_id.in_(
                    [item.id for item in transactions]
                ),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
        ).all() if transactions else []
        match_by_transaction = {
            item.bank_transaction_id: item for item in active_matches
        }
        transaction_projection: list[dict[str, object]] = []
        for transaction in transactions:
            current_late = (
                self._current_late_action(transaction) if transaction.is_late else None
            )
            late_actions = (
                self.session.scalars(
                    select(LateBankEvidenceAction)
                    .where(
                        LateBankEvidenceAction.org_id == request.org_id,
                        LateBankEvidenceAction.bank_transaction_id == transaction.id,
                    )
                    .order_by(
                        LateBankEvidenceAction.created_at,
                        LateBankEvidenceAction.id,
                    )
                ).all()
                if transaction.is_late
                else []
            )
            match = match_by_transaction.get(transaction.id)
            ordinary_state = "not_applicable"
            if not transaction.is_late:
                try:
                    ordinary_state = (
                        "matched"
                        if self._valid_current_match(transaction, match)
                        else "unmatched"
                    )
                except _BankDecision:
                    ordinary_state = "invalid_account_match"
            transaction_projection.append(
                {
                    "transaction_id": str(transaction.id),
                    "bank_account_code": transaction.bank_account_code,
                    "booking_date": transaction.booking_date.isoformat(),
                    "amount_fen": transaction.amount_fen,
                    "external_id": transaction.external_id,
                    "import_action_id": str(transaction.import_action_id),
                    "import_row_number": transaction.import_row_number,
                    "is_late": transaction.is_late,
                    "excluded_from_ordinary_unmatched": transaction.is_late,
                    "original_period_id": str(transaction.original_period_id),
                    "original_close_id": (
                        str(transaction.original_close_id)
                        if transaction.original_close_id is not None
                        else None
                    ),
                    "original_close_hash": transaction.original_close_hash,
                    "original_closed_at": (
                        self._aware(transaction.original_closed_at).isoformat()
                        if transaction.original_closed_at is not None
                        else None
                    ),
                    "late_handling_state": (
                        "handled" if current_late is not None else "pending"
                    )
                    if transaction.is_late
                    else "not_applicable",
                    "current_late_action_id": (
                        str(current_late.id) if current_late is not None else None
                    ),
                    "current_late_action_type": (
                        current_late.action_type if current_late is not None else None
                    ),
                    "ordinary_match_state": ordinary_state,
                    "current_match_event_id": (
                        str(match.event_id) if ordinary_state == "matched" else None
                    ),
                    "late_action_history": [
                        {
                            "action_id": str(action.id),
                            "status": action.status,
                            "action_type": action.action_type,
                            "handling_period_id": (
                                str(action.handling_period_id)
                                if action.handling_period_id is not None
                                else None
                            ),
                            "target_event_id": (
                                str(action.target_event_id)
                                if action.target_event_id is not None
                                else None
                            ),
                            "result_event_id": (
                                str(action.result_event_id)
                                if action.result_event_id is not None
                                else None
                            ),
                            "result_voucher_id": (
                                str(action.result_voucher_id)
                                if action.result_voucher_id is not None
                                else None
                            ),
                            "workflow_name": action.workflow_name,
                            "calculation_hash": action.calculation_hash,
                            "currently_effective": (
                                current_late is not None
                                and action.id == current_late.id
                            ),
                            "created_at": self._aware(action.created_at).isoformat(),
                        }
                        for action in late_actions
                    ],
                }
            )
        scope_accounts = self.session.scalars(
            select(Account)
            .where(
                Account.org_id == request.org_id,
                Account.requires_bank_reconciliation.is_(True),
            )
            .order_by(Account.code, Account.id)
        ).all()
        import_actions: list[dict[str, object]] = []
        if request.include_import_actions:
            import_query = select(BankStatementImportAction).where(
                BankStatementImportAction.org_id == request.org_id
            )
            if request.bank_account_code is not None:
                import_query = import_query.where(
                    BankStatementImportAction.bank_account_code
                    == request.bank_account_code
                )
            import_rows = self.session.scalars(
                import_query.order_by(
                    BankStatementImportAction.created_at.desc(),
                    BankStatementImportAction.id,
                ).limit(request.limit)
            ).all()
            import_actions = [
                {
                    "action_id": str(item.id),
                    "bank_account_code": item.bank_account_code,
                    "status": item.status,
                    "calculation_hash": item.calculation_hash,
                    "row_count": item.row_count,
                    "imported_count": item.imported_count,
                    "duplicate_count": item.duplicate_count,
                    "late_count": item.late_count,
                    "error_count": item.error_count,
                    "created_at": self._aware(item.created_at).isoformat(),
                }
                for item in import_rows
            ]
        reconciliations: list[dict[str, object]] = []
        if request.include_reconciliations:
            reconciliation_query = select(BankReconciliation).where(
                BankReconciliation.org_id == request.org_id
            )
            if request.bank_account_code is not None:
                reconciliation_query = reconciliation_query.where(
                    BankReconciliation.bank_account_code
                    == request.bank_account_code
                )
            reconciliation_rows = self.session.scalars(
                reconciliation_query.order_by(
                    BankReconciliation.confirmed_at.desc(),
                    BankReconciliation.id,
                ).limit(request.limit)
            ).all()
            reconciliations = [
                {
                    "reconciliation_id": str(item.id),
                    "period_id": str(item.period_id),
                    "bank_account_code": item.bank_account_code,
                    "version": item.version,
                    "is_latest_version": item.version
                    == self.session.scalar(
                        select(BankReconciliation.version)
                        .where(
                            BankReconciliation.org_id == item.org_id,
                            BankReconciliation.period_id == item.period_id,
                            BankReconciliation.bank_account_code
                            == item.bank_account_code,
                        )
                        .order_by(BankReconciliation.version.desc())
                        .limit(1)
                    ),
                    "calculation_hash": item.calculation_hash,
                    "statement_to_book_difference_fen": (
                        item.statement_to_book_difference_fen
                    ),
                    "unmatched_transaction_count": item.unmatched_transaction_count,
                    "pending_late_transaction_count": (
                        item.pending_late_transaction_count
                    ),
                    "warnings": item.warnings,
                    "confirmed_at": self._aware(item.confirmed_at).isoformat(),
                }
                for item in reconciliation_rows
            ]
        return {
            "status": "ok",
            "scope": {
                "confirmed": organization.bank_reconciliation_scope_current_action_id
                is not None,
                "current_action_id": (
                    str(organization.bank_reconciliation_scope_current_action_id)
                    if organization.bank_reconciliation_scope_current_action_id
                    else None
                ),
                "confirmed_at": (
                    self._aware(
                        organization.bank_reconciliation_scope_confirmed_at
                    ).isoformat()
                    if organization.bank_reconciliation_scope_confirmed_at
                    else None
                ),
                "accounts": [
                    {
                        "account_id": str(item.id),
                        "bank_account_code": item.code,
                        "account_name": item.name,
                        "start_date": item.bank_reconciliation_start_date.isoformat(),
                        "end_date": (
                            item.bank_reconciliation_end_date.isoformat()
                            if item.bank_reconciliation_end_date
                            else None
                        ),
                    }
                    for item in scope_accounts
                ],
            },
            "historical_scope_corrections_pending": (
                self._historical_scope_corrections(request.org_id)
            ),
            "transactions": transaction_projection,
            "import_actions": import_actions,
            "reconciliations": reconciliations,
        }

    def confirm_bank_reconciliation(
        self,
        request: ConfirmBankReconciliationRequest,
    ) -> BankStatementActionResult:
        attribution_id = self.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
        if not isinstance(attribution_id, uuid.UUID):
            return self._action_error("BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED")
        payload_hash = canonical_sha256(
            {
                "command": "finance_confirm_bank_reconciliation",
                "request": request.model_dump(mode="json"),
            }
        )
        existing = self.session.scalar(
            select(BankReconciliationAction)
            .where(
                BankReconciliationAction.org_id == request.org_id,
                BankReconciliationAction.idempotency_key == request.idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                return self._action_error(
                    "BANK_RECONCILIATION_IDEMPOTENCY_PAYLOAD_MISMATCH"
                )
            return self._replay_reconciliation_action(existing)
        self._lock_tax_period_org(request.org_id)
        period_discovery = self.session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.id == request.period_id,
            )
        )
        if period_discovery is None:
            return self._action_error("BANK_RECONCILIATION_PERIOD_NOT_FOUND")
        self._lock_month(request.org_id, period_discovery.start_date)
        preview_request = PreviewBankReconciliationRequest.model_validate(
            request.model_dump(exclude={"calculation_hash", "idempotency_key"})
        )
        preview = self._bank_reconciliation_preview(preview_request, lock=True)
        if (
            preview.status != BankStatementPreviewStatus.CALCULATED
            or preview.calculation_hash != request.calculation_hash
        ):
            issues = self._reconciliation_issues(preview)
            if preview.status == BankStatementPreviewStatus.CALCULATED:
                issues = [BankStatementIssue(code="BANK_RECONCILIATION_CALCULATION_STALE")]
            return self._persist_reconciliation_rejection(
                request,
                payload_hash,
                issues,
                attribution_id,
            )
        calculation = preview.data.get("calculation")
        if not isinstance(calculation, dict) or preview.calculation_hash is None:
            return self._action_error("BANK_RECONCILIATION_SNAPSHOT_INVALID")
        facts = self._reconciliation_system_facts(preview_request, lock=True)
        try:
            with self.session.begin_nested():
                next_version = (
                    self.session.scalar(
                        select(BankReconciliation.version)
                        .where(
                            BankReconciliation.org_id == request.org_id,
                            BankReconciliation.period_id == request.period_id,
                            BankReconciliation.bank_account_code
                            == request.bank_account_code,
                        )
                        .order_by(BankReconciliation.version.desc())
                        .limit(1)
                    )
                    or 0
                ) + 1
                action = BankReconciliationAction(
                    org_id=request.org_id,
                    period_id=request.period_id,
                    bank_account_code=request.bank_account_code,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=payload_hash,
                    calculation_hash=preview.calculation_hash,
                    status="posted",
                    error_count=0,
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                reconciliation = BankReconciliation(
                    org_id=request.org_id,
                    action_id=action.id,
                    period_id=request.period_id,
                    bank_account_code=request.bank_account_code,
                    version=next_version,
                    calculation=json.loads(canonical_json(calculation)),
                    calculation_payload=canonical_json(calculation),
                    calculation_hash=preview.calculation_hash,
                    coverage_start_date=request.coverage_start_date,
                    coverage_end_date=request.coverage_end_date,
                    statement_opening_balance_fen=request.statement_opening_balance_fen,
                    statement_closing_balance_fen=request.statement_closing_balance_fen,
                    statement_movement_fen=int(calculation["statement_movement_fen"]),
                    statement_integrity_difference_fen=int(
                        calculation["statement_integrity_difference_fen"]
                    ),
                    book_closing_balance_fen=int(calculation["book_closing_balance_fen"]),
                    statement_to_book_difference_fen=int(
                        calculation["statement_to_book_difference_fen"]
                    ),
                    statement_transaction_count=int(
                        calculation["statement_transaction_count"]
                    ),
                    unmatched_transaction_count=facts.unmatched_transaction_count,
                    pending_late_transaction_count=facts.pending_late_transaction_count,
                    warnings=preview.warnings,
                    confirmed_at=self._database_clock(),
                )
                self.session.add(reconciliation)
                self.session.flush()
                self._add_reconciliation_edges(reconciliation, facts)
                self.session.flush()
                self._assert_reconciliation_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.POSTED,
                    action_id=action.id,
                    calculation_hash=action.calculation_hash,
                    trace=[
                        *preview.trace,
                        {"stage": "bank_reconciliation_posted"},
                    ],
                    data={
                        "reconciliation_id": str(reconciliation.id),
                        "version": reconciliation.version,
                        "warnings": preview.warnings,
                    },
                )
        except IntegrityError:
            return self._reconciliation_concurrency_result(request, payload_hash)
        except DBAPIError as exc:
            return self._action_error(self._reconciliation_database_error_code(exc))

    def _import_precondition(
        self,
        request: PreviewBankStatementImportRequest,
        *,
        lock: bool = False,
    ) -> BankStatementImportPreview | None:
        if lock:
            organization = self.session.scalar(
                select(Organization)
                .where(Organization.id == request.org_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        else:
            organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return self._rejected_import_preview("ORGANIZATION_NOT_FOUND")
        # DEC-044 A: no formal bank operation precedes explicit scope confirmation.
        if getattr(organization, "bank_reconciliation_scope_current_action_id", None) is None:
            return BankStatementImportPreview(
                status=BankStatementPreviewStatus.NEEDS_INFORMATION,
                missing_information=[
                    BankStatementInformationRequirement(
                        code="BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                        fields=["bank_reconciliation_scope"],
                    )
                ],
                trace=[{"stage": "bank_scope_confirmation_required"}],
            )
        account = self._bank_account(
            request.org_id,
            request.bank_account_code,
            lock=lock,
        )
        if account is None:
            return self._rejected_import_preview(
                "BANK_ACCOUNT_NOT_CONFIRMED_FOR_RECONCILIATION",
                field_path="bank_account_code",
            )
        return None

    def _bank_scope_preview(
        self,
        request: PreviewBankReconciliationScopeRequest,
        *,
        lock: bool,
    ) -> BankReconciliationScopePreview:
        organization_query = select(Organization).where(Organization.id == request.org_id)
        if lock:
            organization_query = organization_query.with_for_update().execution_options(
                populate_existing=True
            )
        organization = self.session.scalar(organization_query)
        if organization is None:
            return self._scope_preview_error(
                "BANK_RECONCILIATION_SCOPE_ORGANIZATION_NOT_FOUND"
            )
        current_action_id = organization.bank_reconciliation_scope_current_action_id
        if request.action_type == "initial_confirmation":
            if current_action_id is not None:
                return self._scope_preview_error(
                    "BANK_RECONCILIATION_SCOPE_ALREADY_CONFIRMED"
                )
        elif current_action_id != request.previous_action_id:
            return self._scope_preview_error(
                "BANK_RECONCILIATION_SCOPE_VERSION_CONFLICT",
                field_path="previous_action_id",
            )
        account_query = (
            select(Account)
            .where(Account.org_id == request.org_id)
            .order_by(Account.code, Account.id)
        )
        if lock:
            account_query = account_query.with_for_update().execution_options(
                populate_existing=True
            )
        accounts = self.session.scalars(account_query).all()
        by_code = {item.code: item for item in accounts}
        by_id = {item.id: item for item in accounts}
        desired_scope: list[dict[str, object]] = []
        for item in request.accounts:
            existing = by_code.get(item.bank_account_code)
            account_id = (
                existing.id
                if existing is not None
                else self._scope_account_id(request.org_id, item.bank_account_code)
            )
            collision = by_id.get(account_id)
            if collision is not None and collision.code != item.bank_account_code:
                return self._scope_preview_error(
                    "BANK_RECONCILIATION_SCOPE_ACCOUNT_ID_CONFLICT",
                    field_path=f"accounts.{item.bank_account_code}",
                )
            if existing is not None and (
                existing.name != item.account_name
                or not existing.active
                or existing.category != "asset"
                or existing.normal_side != "debit"
            ):
                return self._scope_preview_error(
                    "BANK_RECONCILIATION_SCOPE_ACCOUNT_SHAPE_INVALID",
                    field_path=f"accounts.{item.bank_account_code}",
                )
            desired_scope.append(
                {
                    "account_id": account_id,
                    "bank_account_code": item.bank_account_code,
                    "account_name": item.account_name,
                    "start_date": item.start_date,
                    "end_date": item.end_date,
                }
            )
        desired_scope.sort(
            key=lambda item: (str(item["bank_account_code"]), str(item["account_id"]))
        )
        current_scope = [
            {
                "account_id": item.id,
                "bank_account_code": item.code,
                "account_name": item.name,
                "start_date": item.bank_reconciliation_start_date,
                "end_date": item.bank_reconciliation_end_date,
            }
            for item in accounts
            if item.requires_bank_reconciliation
        ]
        current_scope.sort(
            key=lambda item: (str(item["bank_account_code"]), str(item["account_id"]))
        )
        target_account_id: uuid.UUID | None = None
        if request.action_type == "scope_change":
            current_by_id = {item["account_id"]: item for item in current_scope}
            desired_by_id = {item["account_id"]: item for item in desired_scope}
            changed_ids = {
                account_id
                for account_id in set(current_by_id) | set(desired_by_id)
                if current_by_id.get(account_id) != desired_by_id.get(account_id)
            }
            if len(changed_ids) != 1:
                return self._scope_preview_error(
                    "BANK_RECONCILIATION_SCOPE_CHANGE_MUST_TARGET_ONE_ACCOUNT",
                    field_path="accounts",
                )
            target_account_id = uuid.UUID(str(next(iter(changed_ids))))
        evidence_rows = self.session.scalars(
            select(Evidence)
            .where(Evidence.id.in_(request.evidence_references))
            .order_by(Evidence.id)
        ).all()
        if (
            len(evidence_rows) != len(request.evidence_references)
            or any(item.org_id != request.org_id for item in evidence_rows)
        ):
            return self._scope_preview_error(
                "BANK_RECONCILIATION_SCOPE_EVIDENCE_INVALID",
                field_path="evidence_references",
            )
        affected_closed_periods = self._affected_closed_scope_periods(
            request.org_id,
            current_scope,
            desired_scope,
        )
        payload = {
            "version": "bank-reconciliation-scope-v1",
            "org_id": request.org_id,
            "action_type": request.action_type,
            "previous_action_id": request.previous_action_id,
            "target_account_id": target_account_id,
            "scope": desired_scope,
            "explanation": request.explanation,
            "evidence": [
                {"evidence_id": item.id, "sha256": item.sha256}
                for item in evidence_rows
            ],
            "affected_closed_periods": affected_closed_periods,
        }
        calculation_hash = canonical_sha256(payload)
        warnings = (
            [
                {
                    "code": "BANK_RECONCILIATION_HISTORICAL_SCOPE_CORRECTION",
                    "count": len(affected_closed_periods),
                }
            ]
            if affected_closed_periods
            else []
        )
        return BankReconciliationScopePreview(
            status=BankStatementPreviewStatus.CALCULATED,
            calculation_hash=calculation_hash,
            warnings=warnings,
            trace=[
                {"stage": "bank_scope_facts_validated"},
                {"stage": "calculation_hashed", "calculation_hash": calculation_hash},
            ],
            data={"calculation_payload": payload},
        )

    def _affected_closed_scope_periods(
        self,
        org_id: uuid.UUID,
        current_scope: list[dict[str, object]],
        desired_scope: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        periods = self.session.scalars(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.status == "closed",
            )
            .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
        ).all()

        def effective(scope: list[dict[str, object]], period_end: date) -> set[str]:
            return {
                str(item["bank_account_code"])
                for item in scope
                if isinstance(item["start_date"], date)
                and item["start_date"] <= period_end
                and (
                    item["end_date"] is None
                    or (
                        isinstance(item["end_date"], date)
                        and period_end <= item["end_date"]
                    )
                )
            }

        affected: list[dict[str, object]] = []
        for period in periods:
            old_codes = effective(current_scope, period.end_date)
            new_codes = effective(desired_scope, period.end_date)
            if old_codes == new_codes:
                continue
            affected.append(
                {
                    "period_id": period.id,
                    "period_start_date": period.start_date,
                    "period_end_date": period.end_date,
                    "added_bank_account_codes": sorted(new_codes - old_codes),
                    "removed_bank_account_codes": sorted(old_codes - new_codes),
                }
            )
        return affected

    def _historical_scope_corrections(
        self,
        org_id: uuid.UUID,
    ) -> list[dict[str, object]]:
        histories = self.session.scalars(
            select(AccountBankReconciliationScopeHistory)
            .where(
                AccountBankReconciliationScopeHistory.org_id == org_id,
                AccountBankReconciliationScopeHistory.new_required.is_(True),
            )
            .order_by(
                AccountBankReconciliationScopeHistory.created_at,
                AccountBankReconciliationScopeHistory.id,
            )
        ).all()
        periods = self.session.scalars(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.status == "closed",
            )
        ).all()
        accounts = {
            item.id: item
            for item in self.session.scalars(
                select(Account).where(Account.org_id == org_id)
            ).all()
        }
        pending: dict[tuple[uuid.UUID, uuid.UUID], dict[str, object]] = {}
        for history in histories:
            if history.new_start_date is None:
                continue
            account = accounts.get(history.account_id)
            if account is None:
                continue
            for period in periods:
                if period.close_id is None:
                    continue
                newly_covered = (
                    period.end_date >= history.new_start_date
                    and (
                        history.new_end_date is None
                        or period.end_date <= history.new_end_date
                    )
                    and not (
                        history.old_required
                        and history.old_start_date is not None
                        and period.end_date >= history.old_start_date
                        and (
                            history.old_end_date is None
                            or period.end_date <= history.old_end_date
                        )
                    )
                )
                if not newly_covered:
                    continue
                close = self.session.get(AccountingPeriodClose, period.close_id)
                if (
                    close is None
                    or self._aware(history.created_at)
                    <= self._aware(close.confirmed_at)
                ):
                    continue
                corrected_at = self._aware(history.created_at)
                reconciliation = self.session.scalar(
                    select(BankReconciliation)
                    .where(
                        BankReconciliation.org_id == org_id,
                        BankReconciliation.period_id == period.id,
                        BankReconciliation.bank_account_code == account.code,
                        BankReconciliation.confirmed_at > corrected_at,
                    )
                    .order_by(BankReconciliation.confirmed_at.desc())
                    .limit(1)
                )
                key = (account.id, period.id)
                if reconciliation is None:
                    pending[key] = {
                        "account_id": str(account.id),
                        "bank_account_code": account.code,
                        "period_id": str(period.id),
                        "period_start_date": period.start_date.isoformat(),
                        "period_end_date": period.end_date.isoformat(),
                        "scope_action_id": str(history.scope_action_id),
                        "corrected_at": corrected_at.isoformat(),
                    }
                else:
                    pending.pop(key, None)
        return sorted(
            pending.values(),
            key=lambda item: (
                str(item["period_start_date"]),
                str(item["bank_account_code"]),
            ),
        )

    @staticmethod
    def _scope_account_id(org_id: uuid.UUID, bank_account_code: str) -> uuid.UUID:
        return uuid.uuid5(org_id, f"bank-account:{bank_account_code}")

    def _apply_scope_snapshot(
        self,
        action: BankReconciliationScopeAction,
        request: ConfirmBankReconciliationScopeRequest,
        payload: dict[str, Any],
        attribution_id: uuid.UUID,
    ) -> None:
        now = self._database_clock()
        desired = {
            uuid.UUID(str(item["account_id"])): item for item in payload["scope"]
        }
        existing = self.session.scalars(
            select(Account)
            .where(Account.org_id == request.org_id)
            .order_by(Account.code, Account.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        by_id = {item.id: item for item in existing}
        changed: list[
            tuple[
                Account,
                tuple[bool, date | None, date | None],
                tuple[bool, date | None, date | None],
            ]
        ] = []
        for account_id, item in desired.items():
            start_date = date.fromisoformat(str(item["start_date"]))
            end_date = (
                date.fromisoformat(str(item["end_date"]))
                if item["end_date"] is not None
                else None
            )
            account = by_id.get(account_id)
            if account is None:
                account = Account(
                    id=account_id,
                    org_id=request.org_id,
                    code=str(item["bank_account_code"]),
                    name=str(item["account_name"]),
                    category="asset",
                    normal_side="debit",
                    system_role=None,
                    active=True,
                    requires_bank_reconciliation=True,
                    bank_reconciliation_start_date=start_date,
                    bank_reconciliation_end_date=end_date,
                    bank_reconciliation_configured_at=now,
                )
                self.session.add(account)
                by_id[account_id] = account
                changed.append(
                    (
                        account,
                        (False, None, None),
                        (True, start_date, end_date),
                    )
                )
                continue
            old = (
                account.requires_bank_reconciliation,
                account.bank_reconciliation_start_date,
                account.bank_reconciliation_end_date,
            )
            new = (True, start_date, end_date)
            if old == new:
                continue
            account.requires_bank_reconciliation = True
            account.bank_reconciliation_start_date = start_date
            account.bank_reconciliation_end_date = end_date
            account.bank_reconciliation_configured_at = now
            changed.append((account, old, new))
        for account in existing:
            if not account.requires_bank_reconciliation or account.id in desired:
                continue
            old = (
                True,
                account.bank_reconciliation_start_date,
                account.bank_reconciliation_end_date,
            )
            new = (False, None, None)
            account.requires_bank_reconciliation = False
            account.bank_reconciliation_start_date = None
            account.bank_reconciliation_end_date = None
            account.bank_reconciliation_configured_at = now
            changed.append((account, old, new))
        self.session.flush()
        if self.session.get_bind().dialect.name != "postgresql":
            self.session.add_all(
                [
                    AccountBankReconciliationScopeHistory(
                        org_id=request.org_id,
                        account_id=account.id,
                        scope_action_id=action.id,
                        old_required=old[0],
                        old_start_date=old[1],
                        old_end_date=old[2],
                        new_required=new[0],
                        new_start_date=new[1],
                        new_end_date=new[2],
                        execution_attribution_id=attribution_id,
                        created_at=now,
                    )
                    for account, old, new in changed
                ]
            )

    @staticmethod
    def _scope_issues(
        preview: BankReconciliationScopePreview,
    ) -> list[BankStatementIssue]:
        issues = list(preview.errors)
        for requirement in preview.missing_information:
            issues.extend(
                BankStatementIssue(code=requirement.code, field_path=field)
                for field in (requirement.fields or [None])
            )
        return issues or [BankStatementIssue(code="BANK_RECONCILIATION_SCOPE_REJECTED")]

    @staticmethod
    def _scope_preview_error(
        code: str,
        *,
        field_path: str | None = None,
    ) -> BankReconciliationScopePreview:
        return BankReconciliationScopePreview(
            status=BankStatementPreviewStatus.REJECTED,
            errors=[BankStatementIssue(code=code, field_path=field_path)],
            trace=[{"stage": "bank_scope_preview_rejected", "code": code}],
        )

    def _persist_scope_rejection(
        self,
        request: ConfirmBankReconciliationScopeRequest,
        request_payload_hash: str,
        issue: BankStatementIssue,
        attribution_id: uuid.UUID,
    ) -> BankStatementActionResult:
        if self.session.get(Organization, request.org_id) is None:
            return self._action_error(issue.code, field_path=issue.field_path)
        try:
            with self.session.begin_nested():
                action = BankReconciliationScopeAction(
                    org_id=request.org_id,
                    action_type=None,
                    previous_action_id=None,
                    target_account_id=None,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_payload_hash,
                    calculation_payload=None,
                    calculation_hash=None,
                    scope_snapshot=null(),
                    status="rejected",
                    explanation=None,
                    error_code=issue.code,
                    error_field_path=issue.field_path,
                    error_count=1,
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                self._assert_scope_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.REJECTED,
                    action_id=action.id,
                    errors=[issue],
                )
        except IntegrityError:
            return self._scope_concurrency_result(request, request_payload_hash)
        except DBAPIError as exc:
            return self._action_error(self._scope_database_error_code(exc))

    def _replay_scope_action(
        self,
        action: BankReconciliationScopeAction,
    ) -> BankStatementActionResult:
        return BankStatementActionResult(
            status=(
                BankStatementActionStatus.POSTED
                if action.status == "posted"
                else BankStatementActionStatus.REJECTED
            ),
            action_id=action.id,
            calculation_hash=action.calculation_hash,
            errors=(
                [
                    BankStatementIssue(
                        code=action.error_code
                        or "BANK_RECONCILIATION_SCOPE_REJECTED",
                        field_path=action.error_field_path,
                    )
                ]
                if action.status == "rejected"
                else []
            ),
            trace=[{"stage": "bank_scope_action_idempotent_replay"}],
            data={
                "idempotent_replay": True,
                "scope": action.scope_snapshot if action.status == "posted" else None,
            },
        )

    def _scope_concurrency_result(
        self,
        request: ConfirmBankReconciliationScopeRequest,
        request_payload_hash: str,
    ) -> BankStatementActionResult:
        winner = self.session.scalar(
            select(BankReconciliationScopeAction).where(
                BankReconciliationScopeAction.org_id == request.org_id,
                BankReconciliationScopeAction.idempotency_key == request.idempotency_key,
            )
        )
        if winner is None:
            return self._action_error(
                "BANK_RECONCILIATION_SCOPE_CONCURRENT_WRITE_CONFLICT"
            )
        if winner.request_payload_hash != request_payload_hash:
            return self._action_error(
                "BANK_RECONCILIATION_SCOPE_IDEMPOTENCY_PAYLOAD_MISMATCH"
            )
        return self._replay_scope_action(winner)

    def _late_bank_preview(
        self,
        request: PreviewLateBankEvidenceRequest,
        *,
        lock: bool,
    ) -> LateBankEvidencePreview:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return self._late_preview_error("LATE_BANK_EVIDENCE_ORGANIZATION_NOT_FOUND")
        if organization.bank_reconciliation_scope_current_action_id is None:
            return LateBankEvidencePreview(
                status=BankStatementPreviewStatus.NEEDS_INFORMATION,
                missing_information=[
                    BankStatementInformationRequirement(
                        code="LATE_BANK_EVIDENCE_SCOPE_CONFIRMATION_REQUIRED",
                        fields=["bank_reconciliation_scope"],
                    )
                ],
            )
        discovery = self.session.scalar(
            select(BankTransaction).where(
                BankTransaction.org_id == request.org_id,
                BankTransaction.id == request.bank_transaction_id,
            )
        )
        if discovery is None:
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_TRANSACTION_NOT_FOUND",
                field_path="bank_transaction_id",
            )
        handling_discovery = (
            self.session.scalar(
                select(AccountingPeriod).where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.id == request.handling_period_id,
                )
            )
            if request.handling_period_id is not None
            else None
        )
        if lock:
            self._lock_month(request.org_id, discovery.booking_date.replace(day=1))
            if handling_discovery is not None:
                self._lock_month(request.org_id, handling_discovery.start_date)
            transaction_query = (
                select(BankTransaction)
                .where(
                    BankTransaction.org_id == request.org_id,
                    BankTransaction.id == request.bank_transaction_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            transaction = self.session.scalar(transaction_query)
        else:
            transaction = discovery
        if transaction is None or not transaction.is_late:
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_ORIGINAL_PERIOD_NOT_CLOSED",
                field_path="bank_transaction_id",
            )
        original_period = self.session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.id == transaction.original_period_id,
            )
        )
        if (
            original_period is None
            or original_period.status != "closed"
            or original_period.close_id != transaction.original_close_id
        ):
            return self._late_preview_error("LATE_BANK_EVIDENCE_ORIGINAL_PERIOD_NOT_CLOSED")
        active_action = self._current_late_action(transaction)
        if active_action is not None:
            return self._late_preview_error("LATE_BANK_EVIDENCE_ALREADY_HANDLED")

        missing_fields: list[str] = []
        if request.handling_period_id is None:
            missing_fields.append("handling_period_id")
        if request.explanation is None:
            missing_fields.append("explanation")
        if not request.evidence_references:
            missing_fields.append("evidence_references")
        if request.action_type == "evidence_only" and request.target_event_id is None:
            missing_fields.append("target_event_id")
        if request.action_type == "omitted_entry":
            if request.result_event_id is None:
                missing_fields.append("result_event_id")
            if request.result_voucher_id is None:
                missing_fields.append("result_voucher_id")
        if missing_fields:
            return LateBankEvidencePreview(
                status=BankStatementPreviewStatus.NEEDS_INFORMATION,
                missing_information=[
                    BankStatementInformationRequirement(
                        code="LATE_BANK_EVIDENCE_INFORMATION_REQUIRED",
                        fields=missing_fields,
                    )
                ],
            )
        if len(request.evidence_references) != len(set(request.evidence_references)):
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_DUPLICATE_EVIDENCE",
                field_path="evidence_references",
            )
        handling = self.session.scalar(

                select(AccountingPeriod)
                .where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.id == request.handling_period_id,
                )
                .with_for_update()
                if lock
                else select(AccountingPeriod).where(
                    AccountingPeriod.org_id == request.org_id,
                    AccountingPeriod.id == request.handling_period_id,
                )

        )
        if handling is None or handling.status != "open":
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_HANDLING_PERIOD_NOT_OPEN",
                field_path="handling_period_id",
            )
        if handling.start_date <= original_period.start_date:
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_HANDLING_PERIOD_NOT_AFTER_ORIGINAL",
                field_path="handling_period_id",
            )
        if handling.start_date > self._today().replace(day=1):
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_HANDLING_PERIOD_FUTURE_NOT_ALLOWED",
                field_path="handling_period_id",
            )
        evidence_rows = self.session.scalars(
            select(Evidence)
            .where(Evidence.id.in_(request.evidence_references))
            .order_by(Evidence.id)
        ).all()
        if len(evidence_rows) != len(request.evidence_references) or any(
            item.org_id != request.org_id for item in evidence_rows
        ):
            return self._late_preview_error(
                "LATE_BANK_EVIDENCE_CROSS_ORGANIZATION_REFERENCE",
                field_path="evidence_references",
            )

        workflow_name: str | None = None
        if request.action_type == "evidence_only":
            source = self.session.scalar(
                select(AccountingPeriodCloseSource).where(
                    AccountingPeriodCloseSource.org_id == request.org_id,
                    AccountingPeriodCloseSource.close_id == transaction.original_close_id,
                    AccountingPeriodCloseSource.event_id == request.target_event_id,
                )
            )
            event = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.id == request.target_event_id,
                )
            )
            if source is None or event is None or event.status != "posted":
                return self._late_preview_error(
                    "LATE_BANK_EVIDENCE_TARGET_EVENT_INVALID",
                    field_path="target_event_id",
                )
            movement = sum(
                int(line["debit_fen"]) - int(line["credit_fen"])
                for line in source.line_snapshot
                if line.get("account_code") == transaction.bank_account_code
            )
            if movement != transaction.amount_fen:
                return self._late_preview_error(
                    "LATE_BANK_EVIDENCE_TARGET_EVENT_AMOUNT_MISMATCH",
                    field_path="target_event_id",
                )
        else:
            event = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.id == request.result_event_id,
                )
            )
            voucher = self.session.scalar(
                select(Voucher).where(
                    Voucher.org_id == request.org_id,
                    Voucher.id == request.result_voucher_id,
                    Voucher.event_id == request.result_event_id,
                )
            )
            if (
                event is None
                or voucher is None
                or event.status != "posted"
                or voucher.status != "posted"
                or event.posting_date < handling.start_date
                or event.posting_date > handling.end_date
            ):
                return self._late_preview_error(
                    "LATE_BANK_EVIDENCE_TYPED_WORKFLOW_RESULT_INVALID",
                    field_path="result_event_id",
                )
            movement = self._voucher_bank_movement(voucher.id, transaction.bank_account_code)
            if movement != transaction.amount_fen:
                return self._late_preview_error(
                    "LATE_BANK_EVIDENCE_TYPED_WORKFLOW_AMOUNT_MISMATCH",
                    field_path="result_voucher_id",
                )
            workflow_name = event.event_type
        payload = {
            "version": "late-bank-evidence-action-v1",
            "org_id": request.org_id,
            "bank_transaction_id": request.bank_transaction_id,
            "action_type": request.action_type,
            "handling_period_id": request.handling_period_id,
            "original_close_id": transaction.original_close_id,
            "original_close_hash": transaction.original_close_hash,
            "target_event_id": request.target_event_id,
            "result_event_id": request.result_event_id,
            "result_voucher_id": request.result_voucher_id,
            "workflow_name": workflow_name,
            "explanation": request.explanation,
            "evidence": [
                {"evidence_id": item.id, "sha256": item.sha256}
                for item in sorted(evidence_rows, key=lambda item: str(item.id))
            ],
        }
        calculation_hash = canonical_sha256(payload)
        return LateBankEvidencePreview(
            status=BankStatementPreviewStatus.CALCULATED,
            calculation_hash=calculation_hash,
            trace=[
                {"stage": "late_bank_evidence_facts_validated"},
                {"stage": "calculation_hashed", "calculation_hash": calculation_hash},
            ],
            data={"calculation_payload": payload},
        )

    def _bank_reconciliation_preview(
        self,
        request: PreviewBankReconciliationRequest,
        *,
        lock: bool,
    ) -> BankReconciliationPreview:
        try:
            facts = self._reconciliation_system_facts(request, lock=lock)
        except _BankDecision as exc:
            return BankReconciliationPreview(
                status=BankStatementPreviewStatus.REJECTED,
                errors=[
                    BankStatementIssue(code=exc.code, field_path=exc.field_path)
                ],
                trace=[{"stage": "bank_reconciliation_rejected", "code": exc.code}],
            )
        return calculate_bank_reconciliation(request, facts)

    def _reconciliation_system_facts(
        self,
        request: PreviewBankReconciliationRequest,
        *,
        lock: bool,
    ) -> BankReconciliationSystemFacts:
        organization_query = select(Organization).where(Organization.id == request.org_id)
        if lock:
            organization_query = organization_query.with_for_update().execution_options(
                populate_existing=True
            )
        organization = self.session.scalar(organization_query)
        if organization is None:
            raise _BankDecision("BANK_RECONCILIATION_ORGANIZATION_NOT_FOUND")
        if organization.bank_reconciliation_scope_current_action_id is None:
            raise _BankDecision("BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED")
        period_query = select(AccountingPeriod).where(
            AccountingPeriod.org_id == request.org_id,
            AccountingPeriod.id == request.period_id,
        )
        if lock:
            period_query = period_query.with_for_update().execution_options(
                populate_existing=True
            )
        period = self.session.scalar(period_query)
        if period is None:
            raise _BankDecision(
                "BANK_RECONCILIATION_PERIOD_NOT_FOUND", field_path="period_id"
            )
        if period.status != "open":
            raise _BankDecision(
                "BANK_RECONCILIATION_PERIOD_NOT_OPEN", field_path="period_id"
            )
        if period.start_date > self._today().replace(day=1):
            raise _BankDecision(
                "BANK_RECONCILIATION_FUTURE_PERIOD_NOT_ALLOWED",
                field_path="period_id",
            )
        account = self._bank_account(
            request.org_id,
            request.bank_account_code,
            lock=lock,
        )
        if account is None:
            raise _BankDecision(
                "BANK_ACCOUNT_NOT_CONFIRMED_FOR_RECONCILIATION",
                field_path="bank_account_code",
            )
        if (
            account.bank_reconciliation_start_date > period.start_date
            or (
                account.bank_reconciliation_end_date is not None
                and account.bank_reconciliation_end_date < period.end_date
            )
        ):
            raise _BankDecision(
                "BANK_ACCOUNT_RECONCILIATION_SCOPE_NOT_EFFECTIVE",
                field_path="bank_account_code",
            )
        transaction_query = (
            select(BankTransaction)
            .where(
                BankTransaction.org_id == request.org_id,
                BankTransaction.bank_account_code == request.bank_account_code,
                BankTransaction.booking_date.between(period.start_date, period.end_date),
            )
            .order_by(BankTransaction.booking_date, BankTransaction.id)
        )
        if lock:
            transaction_query = transaction_query.with_for_update()
        transactions = self.session.scalars(transaction_query).all()
        if any(item.import_action_id is None for item in transactions):
            raise _BankDecision(
                "BANK_RECONCILIATION_LEGACY_TRANSACTION_REQUIRES_MIGRATION"
            )
        required_action_ids = {item.import_action_id for item in transactions}
        requested_action_ids = set(request.statement_import_action_ids)
        if required_action_ids != requested_action_ids:
            raise _BankDecision(
                "BANK_RECONCILIATION_INCOMPLETE_IMPORT_ACTION_SET",
                field_path="statement_import_action_ids",
            )
        action_query = select(BankStatementImportAction).where(
            BankStatementImportAction.org_id == request.org_id,
            BankStatementImportAction.id.in_(request.statement_import_action_ids),
        )
        if lock:
            action_query = action_query.with_for_update()
        actions = self.session.scalars(action_query).all()
        if (
            len(actions) != len(request.statement_import_action_ids)
            or any(
                action.status not in {"posted", "partially_posted"}
                or action.bank_account_code != request.bank_account_code
                or action.calculation_hash is None
                for action in actions
            )
        ):
            raise _BankDecision(
                "BANK_RECONCILIATION_IMPORT_ACTION_SCOPE_MISMATCH",
                field_path="statement_import_action_ids",
            )
        by_action: dict[uuid.UUID, list[BankTransaction]] = {
            action.id: [] for action in actions
        }
        for transaction in transactions:
            assert transaction.import_action_id is not None
            by_action[transaction.import_action_id].append(transaction)
        evidence_rows = self.session.scalars(
            select(Evidence)
            .where(Evidence.id.in_(request.statement_evidence_references))
            .order_by(Evidence.id)
        ).all()
        if (
            len(evidence_rows) != len(request.statement_evidence_references)
            or any(item.org_id != request.org_id for item in evidence_rows)
        ):
            raise _BankDecision(
                "BANK_RECONCILIATION_EVIDENCE_SCOPE_MISMATCH",
                field_path="statement_evidence_references",
            )
        ledger_rows = self.session.execute(
            select(VoucherLine.debit_fen, VoucherLine.credit_fen)
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .join(Account, Account.id == VoucherLine.account_id)
            .where(
                VoucherLine.org_id == request.org_id,
                Account.org_id == request.org_id,
                Account.code == request.bank_account_code,
                Voucher.posting_date <= period.end_date,
                Voucher.status.in_(("posted", "reversed")),
            )
        ).all()
        book_closing = sum(int(debit) - int(credit) for debit, credit in ledger_rows)
        cumulative_ordinary = self.session.scalars(
            select(BankTransaction).where(
                BankTransaction.org_id == request.org_id,
                BankTransaction.bank_account_code == request.bank_account_code,
                BankTransaction.booking_date <= period.end_date,
                BankTransaction.is_late.is_(False),
            )
        ).all()
        active_matches = self.session.scalars(
            select(BankTransactionMatch).where(
                BankTransactionMatch.org_id == request.org_id,
                BankTransactionMatch.bank_transaction_id.in_(
                    [item.id for item in cumulative_ordinary]
                ),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
        ).all() if cumulative_ordinary else []
        active_by_transaction = {
            item.bank_transaction_id: item for item in active_matches
        }
        unmatched = sum(
            1
            for transaction in cumulative_ordinary
            if not self._valid_current_match(
                transaction,
                active_by_transaction.get(transaction.id),
            )
        )
        pending_late = 0
        late_rows = self.session.scalars(
            select(BankTransaction).where(
                BankTransaction.org_id == request.org_id,
                BankTransaction.bank_account_code == request.bank_account_code,
                BankTransaction.is_late.is_(True),
            )
        ).all()
        for late in late_rows:
            original = self.session.get(AccountingPeriod, late.original_period_id)
            if (
                original is not None
                and original.start_date < period.start_date
                and self._current_late_action(late) is None
            ):
                pending_late += 1
        return BankReconciliationSystemFacts(
            org_id=request.org_id,
            period_id=request.period_id,
            bank_account_code=request.bank_account_code,
            period_start_date=period.start_date,
            period_end_date=period.end_date,
            book_closing_balance_fen=book_closing,
            unmatched_transaction_count=unmatched,
            pending_late_transaction_count=pending_late,
            import_actions=[
                BankReconciliationImportActionFact(
                    action_id=action.id,
                    org_id=action.org_id,
                    bank_account_code=action.bank_account_code,
                    status=action.status,
                    request_payload_hash=action.request_payload_hash,
                    calculation_hash=action.calculation_hash,
                    transactions=[
                        BankReconciliationImportedTransactionFact(
                            transaction_id=transaction.id,
                            booking_date=transaction.booking_date,
                            amount_fen=transaction.amount_fen,
                        )
                        for transaction in by_action[action.id]
                    ],
                )
                for action in sorted(actions, key=lambda item: str(item.id))
            ],
            statement_evidence=[
                BankReconciliationEvidenceFact(
                    evidence_id=item.id,
                    org_id=item.org_id,
                    sha256=item.sha256,
                )
                for item in evidence_rows
            ],
        )

    def _current_late_action(
        self,
        transaction: BankTransaction,
    ) -> LateBankEvidenceAction | None:
        actions = self.session.scalars(
            select(LateBankEvidenceAction)
            .where(
                LateBankEvidenceAction.org_id == transaction.org_id,
                LateBankEvidenceAction.bank_transaction_id == transaction.id,
                LateBankEvidenceAction.status == "posted",
            )
            .order_by(LateBankEvidenceAction.created_at.desc(), LateBankEvidenceAction.id.desc())
        ).all()
        for action in actions:
            direct_event_id = (
                action.target_event_id
                if action.action_type == "evidence_only"
                else action.result_event_id
            )
            direct_status = self.session.scalar(
                select(BusinessEvent.status).where(
                    BusinessEvent.org_id == transaction.org_id,
                    BusinessEvent.id == direct_event_id,
                )
            )
            # DEC-037: reversal removes only this direct action's current
            # effect.  Older history remains and is evaluated independently;
            # replacement is never discovered automatically.
            if direct_status == "posted":
                return action
        return None

    def _valid_current_match(
        self,
        transaction: BankTransaction,
        match: BankTransactionMatch | None,
    ) -> bool:
        if match is None:
            return False
        event = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == transaction.org_id,
                BusinessEvent.id == match.event_id,
            )
        )
        if event is None or event.status != "posted":
            return False
        voucher = self.session.scalar(
            select(Voucher).where(
                Voucher.org_id == transaction.org_id,
                Voucher.event_id == event.id,
                Voucher.status == "posted",
            )
        )
        if (
            voucher is None
            or self._voucher_bank_movement(voucher.id, transaction.bank_account_code)
            != transaction.amount_fen
        ):
            raise _BankDecision(
                "BANK_RECONCILIATION_MATCHED_EVENT_BANK_ACCOUNT_MISMATCH"
            )
        return True

    def _voucher_bank_movement(self, voucher_id: uuid.UUID, account_code: str) -> int:
        rows = self.session.execute(
            select(VoucherLine.debit_fen, VoucherLine.credit_fen)
            .join(Account, Account.id == VoucherLine.account_id)
            .where(
                VoucherLine.voucher_id == voucher_id,
                Account.code == account_code,
            )
        ).all()
        return sum(int(debit) - int(credit) for debit, credit in rows)

    def _bank_account(
        self,
        org_id: uuid.UUID,
        code: str,
        *,
        lock: bool = False,
    ) -> Account | None:
        query = select(Account).where(Account.org_id == org_id, Account.code == code)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        account = self.session.scalar(query)
        if account is None:
            return None
        if (
            not account.active
            or account.category != "asset"
            or account.normal_side != "debit"
            or not getattr(account, "requires_bank_reconciliation", False)
            or getattr(account, "bank_reconciliation_start_date", None) is None
            or getattr(account, "bank_reconciliation_configured_at", None) is None
        ):
            return None
        return account

    def _validate_account_dates(
        self,
        account: Account,
        parsed: ParsedBankStatement,
    ) -> Any | None:
        from .bank_statement_schemas import BankStatementInformationRequirement

        start = account.bank_reconciliation_start_date
        end = account.bank_reconciliation_end_date
        invalid = [
            row
            for row in parsed.rows
            if start is None
            or row.booking_date < start
            or (end is not None and row.booking_date > end)
        ]
        if not invalid:
            return None
        return BankStatementInformationRequirement(
            code="BANK_ACCOUNT_RECONCILIATION_SCOPE_NOT_EFFECTIVE",
            fields=[f"rows.{row.row_identity_sha256}.booking_date" for row in invalid],
        )

    def _read_controlled_csv(self, source_file_name: str) -> bytes:
        root = self.settings.finance_bank_import_dir
        if root is None:
            raise PathSecurityError("FILE_IMPORT_ROOT_UNAVAILABLE")
        if Path(source_file_name).suffix.casefold() != ".csv":
            raise PathSecurityError("FILE_FORMAT_NOT_ALLOWED")
        _, content = read_regular_file_in_root(
            root / source_file_name,
            root,
            max_bytes=self.settings.finance_max_bank_import_bytes,
        )
        return content

    def _import_system_facts(
        self,
        request: PreviewBankStatementImportRequest,
        parsed: ParsedBankStatement,
        *,
        lock_periods: bool = False,
    ) -> BankStatementImportSystemFacts:
        row_identities = [row.row_identity_sha256 for row in parsed.rows]
        existing_by_row = (
            {
                row.row_identity_sha256: row
                for row in self.session.scalars(
                    select(BankTransaction).where(
                        BankTransaction.org_id == request.org_id,
                        BankTransaction.bank_account_code == request.bank_account_code,
                        BankTransaction.row_identity_sha256.in_(row_identities),
                    )
                ).all()
            }
            if row_identities
            else {}
        )
        external_ids = [row.external_id for row in parsed.rows if row.external_id is not None]
        existing_by_external = (
            {
                row.external_id: row
                for row in self.session.scalars(
                    select(BankTransaction).where(
                        BankTransaction.org_id == request.org_id,
                        BankTransaction.bank_account_code == request.bank_account_code,
                        BankTransaction.external_id.in_(external_ids),
                    )
                ).all()
                if row.external_id is not None
            }
            if external_ids
            else {}
        )
        manual_ids = [
            item.duplicate_bank_transaction_id
            for item in request.missing_external_id_resolutions
            if item.duplicate_bank_transaction_id is not None
        ]
        manual_by_id = (
            {
                row.id: row
                for row in self.session.scalars(
                    select(BankTransaction).where(BankTransaction.id.in_(manual_ids))
                ).all()
            }
            if manual_ids
            else {}
        )
        evidence_ids = sorted(
            {
                evidence_id
                for resolution in request.missing_external_id_resolutions
                for evidence_id in resolution.evidence_references
            },
            key=str,
        )
        evidence = (
            self.session.scalars(
                select(Evidence).where(Evidence.id.in_(evidence_ids)).order_by(Evidence.id)
            ).all()
            if evidence_ids
            else []
        )
        periods = self._periods_by_month(
            request.org_id,
            parsed,
            lock=lock_periods,
        )

        row_facts: list[BankStatementImportRowSystemFact] = []
        for row in parsed.rows:
            period = periods.get(row.booking_date.replace(day=1))
            row_facts.append(
                BankStatementImportRowSystemFact(
                    row_identity_sha256=row.row_identity_sha256,
                    period=self._period_projection(row.booking_date, period),
                    existing_source_row_transaction=self._snapshot(
                        existing_by_row.get(row.row_identity_sha256)
                    ),
                    existing_external_id_transaction=self._snapshot(
                        existing_by_external.get(row.external_id)
                        if row.external_id is not None
                        else None
                    ),
                    manual_duplicate_target=self._snapshot(
                        manual_by_id.get(
                            next(
                                (
                                    item.duplicate_bank_transaction_id
                                    for item in request.missing_external_id_resolutions
                                    if item.row_identity_sha256 == row.row_identity_sha256
                                ),
                                None,
                            )
                        )
                    ),
                )
            )
        return BankStatementImportSystemFacts(
            org_id=request.org_id,
            bank_account_code=request.bank_account_code,
            as_of_date=self._today(),
            rows=row_facts,
            resolution_evidence=[
                BankStatementImportEvidenceFact(
                    evidence_id=item.id,
                    org_id=item.org_id,
                    sha256=item.sha256,
                )
                for item in evidence
            ],
        )

    def _periods_by_month(
        self,
        org_id: uuid.UUID,
        parsed: ParsedBankStatement,
        *,
        lock: bool = False,
    ) -> dict[date, AccountingPeriod]:
        months = sorted({row.booking_date.replace(day=1) for row in parsed.rows})
        if not months:
            return {}
        query = select(AccountingPeriod).where(
            AccountingPeriod.org_id == org_id,
            AccountingPeriod.start_date.in_(months),
        )
        if lock:
            query = query.order_by(
                AccountingPeriod.start_date, AccountingPeriod.id
            ).with_for_update()
        rows = self.session.scalars(query).all()
        return {row.start_date: row for row in rows}

    def _persist_import_success(
        self,
        request: ConfirmBankStatementFileImportRequest,
        parsed: ParsedBankStatement,
        preview: BankStatementImportPreview,
        request_payload_hash: str,
        attribution_id: uuid.UUID,
    ) -> BankStatementActionResult:
        payload = preview.data.get("calculation_payload")
        if not isinstance(payload, dict) or preview.calculation_hash is None:
            return self._action_error("BANK_STATEMENT_IMPORT_SNAPSHOT_INVALID")
        status = str(preview.data["planned_confirm_status"])
        calculation_payload = canonical_json(payload)
        normalized_result = json.loads(calculation_payload)
        action = BankStatementImportAction(
            org_id=request.org_id,
            bank_account_code=request.bank_account_code,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_payload_hash,
            source_sha256=parsed.source_sha256,
            parser_request_fingerprint_sha256=parsed.parser_request_fingerprint_sha256,
            calculation_payload=calculation_payload,
            calculation_hash=preview.calculation_hash,
            status=status,
            file_format="csv",
            column_mapping=request.column_mapping.model_dump(mode="json", exclude_none=True),
            normalized_result=normalized_result,
            row_count=int(preview.data["row_count"]),
            valid_row_count=int(preview.data["valid_row_count"]),
            imported_count=int(preview.data["planned_import_count"]),
            duplicate_count=int(preview.data["planned_duplicate_count"]),
            late_count=int(preview.data["late_import_count"]),
            error_count=int(preview.data["row_error_count"]),
            execution_attribution_id=attribution_id,
        )
        self.session.add(action)
        self.session.flush()
        imported_at = self._database_clock()
        imported_rows: list[BankTransaction] = []
        duplicates: list[uuid.UUID] = []
        for row in preview.rows:
            if row.disposition in {"stable_duplicate", "manual_duplicate"}:
                if row.duplicate_bank_transaction_id is not None:
                    duplicates.append(row.duplicate_bank_transaction_id)
                continue
            if row.disposition not in {"ready", "manual_new"}:
                continue
            transaction = BankTransaction(
                org_id=request.org_id,
                bank_account_code=request.bank_account_code,
                fingerprint=canonical_sha256(
                    {
                        "version": "bank-transaction-fingerprint-v2",
                        "org_id": request.org_id,
                        "bank_account_code": request.bank_account_code,
                        "external_id": row.external_id,
                        "row_identity_sha256": row.row_identity_sha256,
                    }
                ),
                external_id=row.external_id,
                booking_date=row.booking_date,
                amount_fen=row.amount_fen,
                currency=row.currency,
                counterparty_name=row.counterparty_name,
                memo=row.memo,
                source_sha256=parsed.source_sha256,
                import_action_id=action.id,
                import_row_number=row.row_number,
                row_identity_sha256=row.row_identity_sha256,
                original_period_id=row.period_id,
                is_late=row.is_late,
                original_close_id=row.original_close_id,
                original_close_hash=row.original_close_hash,
                original_closed_at=row.original_closed_at,
                execution_attribution_id=attribution_id,
                imported_at=imported_at,
            )
            self.session.add(transaction)
            imported_rows.append(transaction)
        resolution_evidence = normalized_result.get("system_facts", {}).get(
            "resolution_evidence", []
        )
        self.session.add_all(
            [
                BankStatementImportActionEvidence(
                    org_id=request.org_id,
                    action_id=action.id,
                    evidence_id=uuid.UUID(str(item["evidence_id"])),
                    evidence_sha256_at_import=str(item["sha256"]),
                )
                for item in resolution_evidence
            ]
        )
        self._add_import_failures(action, preview.errors)
        self.session.flush()
        imported = [item.id for item in imported_rows]
        return BankStatementActionResult(
            status=BankStatementActionStatus(status),
            action_id=action.id,
            calculation_hash=preview.calculation_hash,
            errors=preview.errors,
            trace=[
                *preview.trace,
                {
                    "stage": "bank_statement_import_posted",
                    "action_id": str(action.id),
                },
            ],
            data={
                "source_sha256": parsed.source_sha256,
                "imported_count": len(imported),
                "duplicate_count": len(duplicates),
                "error_count": len(preview.errors),
                "late_count": int(preview.data["late_import_count"]),
                "imported_transaction_ids": [str(item) for item in imported],
                "duplicate_transaction_ids": [str(item) for item in duplicates],
            },
        )

    def _persist_import_rejection(
        self,
        request: ConfirmBankStatementFileImportRequest,
        parsed: ParsedBankStatement,
        request_payload_hash: str,
        issues: list[BankStatementIssue],
        attribution_id: uuid.UUID,
    ) -> BankStatementActionResult:
        issues = issues or [BankStatementIssue(code="BANK_STATEMENT_CONFIRMATION_REJECTED")]
        row_count = int(parsed.data.get("row_count", len(parsed.rows) + len(parsed.errors)))
        action = BankStatementImportAction(
            org_id=request.org_id,
            bank_account_code=request.bank_account_code,
            idempotency_key=request.idempotency_key,
            request_payload_hash=request_payload_hash,
            source_sha256=parsed.source_sha256,
            parser_request_fingerprint_sha256=parsed.parser_request_fingerprint_sha256,
            calculation_payload=None,
            calculation_hash=None,
            status="rejected",
            file_format=None,
            column_mapping=null(),
            normalized_result=null(),
            row_count=row_count,
            valid_row_count=0,
            imported_count=0,
            duplicate_count=0,
            late_count=0,
            error_count=len(issues),
            execution_attribution_id=attribution_id,
        )
        self.session.add(action)
        self.session.flush()
        self._add_import_failures(action, issues)
        self.session.flush()
        return BankStatementActionResult(
            status=BankStatementActionStatus.REJECTED,
            action_id=action.id,
            errors=issues,
            trace=[{"stage": "bank_statement_confirmation_rejected"}],
        )

    def _persist_import_unavailable_rejection(
        self,
        request: ConfirmBankStatementFileImportRequest,
        request_payload_hash: str,
        issue: BankStatementIssue,
        attribution_id: uuid.UUID,
    ) -> BankStatementActionResult:
        existing = self.session.scalar(
            select(BankStatementImportAction)
            .where(
                BankStatementImportAction.org_id == request.org_id,
                BankStatementImportAction.idempotency_key == request.idempotency_key,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.request_payload_hash != request_payload_hash:
                return self._action_error("BANK_STATEMENT_IDEMPOTENCY_PAYLOAD_MISMATCH")
            return self._replay_import_action(existing)
        try:
            with self.session.begin_nested():
                action = BankStatementImportAction(
                    org_id=request.org_id,
                    bank_account_code=request.bank_account_code,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_payload_hash,
                    source_sha256=None,
                    parser_request_fingerprint_sha256=None,
                    calculation_payload=None,
                    calculation_hash=None,
                    status="rejected",
                    file_format=None,
                    column_mapping=null(),
                    normalized_result=null(),
                    row_count=0,
                    valid_row_count=0,
                    imported_count=0,
                    duplicate_count=0,
                    late_count=0,
                    error_count=1,
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                self._add_import_failures(action, [issue])
                self.session.flush()
                self._assert_import_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.REJECTED,
                    action_id=action.id,
                    errors=[issue],
                    trace=[{"stage": "bank_statement_input_failure_recorded"}],
                )
        except IntegrityError:
            return self._import_concurrency_result(
                request.org_id,
                request.idempotency_key,
                request_payload_hash,
            )
        except DBAPIError as exc:
            return self._action_error(self._bank_database_error_code(exc))

    def _add_import_failures(
        self,
        action: BankStatementImportAction,
        issues: list[BankStatementIssue],
    ) -> None:
        self.session.add_all(
            [
                BankStatementImportFailure(
                    org_id=action.org_id,
                    action_id=action.id,
                    error_ordinal=index,
                    code=issue.code,
                    row_number=issue.row_number,
                    field_path=issue.field_path,
                )
                for index, issue in enumerate(issues, start=1)
            ]
        )

    def _replay_import_action(
        self,
        action: BankStatementImportAction,
    ) -> BankStatementActionResult:
        failures = self.session.scalars(
            select(BankStatementImportFailure)
            .where(
                BankStatementImportFailure.org_id == action.org_id,
                BankStatementImportFailure.action_id == action.id,
            )
            .order_by(BankStatementImportFailure.error_ordinal)
        ).all()
        transactions = self.session.scalars(
            select(BankTransaction)
            .where(
                BankTransaction.org_id == action.org_id,
                BankTransaction.import_action_id == action.id,
            )
            .order_by(BankTransaction.import_row_number, BankTransaction.id)
        ).all()
        return BankStatementActionResult(
            status=BankStatementActionStatus(action.status),
            action_id=action.id,
            calculation_hash=action.calculation_hash,
            errors=[
                BankStatementIssue(
                    code=item.code,
                    row_number=item.row_number,
                    field_path=item.field_path,
                )
                for item in failures
            ],
            trace=[{"stage": "bank_statement_import_idempotent_replay"}],
            data={
                "source_sha256": action.source_sha256,
                "imported_count": action.imported_count,
                "duplicate_count": action.duplicate_count,
                "error_count": action.error_count,
                "late_count": action.late_count,
                "imported_transaction_ids": [str(item.id) for item in transactions],
                "idempotent_replay": True,
            },
        )

    def _import_concurrency_result(
        self,
        org_id: uuid.UUID,
        idempotency_key: str,
        request_payload_hash: str,
    ) -> BankStatementActionResult:
        winner = self.session.scalar(
            select(BankStatementImportAction).where(
                BankStatementImportAction.org_id == org_id,
                BankStatementImportAction.idempotency_key == idempotency_key,
            )
        )
        if winner is None:
            return self._action_error("BANK_STATEMENT_CONCURRENT_WRITE_CONFLICT")
        if winner.request_payload_hash != request_payload_hash:
            return self._action_error("BANK_STATEMENT_IDEMPOTENCY_PAYLOAD_MISMATCH")
        return self._replay_import_action(winner)

    @staticmethod
    def _late_preview_error(
        code: str,
        *,
        field_path: str | None = None,
    ) -> LateBankEvidencePreview:
        return LateBankEvidencePreview(
            status=BankStatementPreviewStatus.REJECTED,
            errors=[BankStatementIssue(code=code, field_path=field_path)],
            trace=[{"stage": "late_bank_evidence_rejected", "code": code}],
        )

    @staticmethod
    def _late_failure(preview: LateBankEvidencePreview) -> tuple[str, str | None]:
        if preview.errors:
            return preview.errors[0].code, preview.errors[0].field_path
        if preview.missing_information:
            requirement = preview.missing_information[0]
            return requirement.code, requirement.fields[0] if requirement.fields else None
        return "LATE_BANK_EVIDENCE_CONFIRMATION_REJECTED", None

    def _persist_late_rejection(
        self,
        request: ConfirmLateBankEvidenceRequest,
        request_payload_hash: str,
        code: str,
        field_path: str | None,
        attribution_id: uuid.UUID,
    ) -> BankStatementActionResult:
        transaction = self.session.scalar(
            select(BankTransaction).where(
                BankTransaction.org_id == request.org_id,
                BankTransaction.id == request.bank_transaction_id,
            )
        )
        if transaction is None:
            return self._action_error(code, field_path=field_path)
        try:
            with self.session.begin_nested():
                action = LateBankEvidenceAction(
                    org_id=request.org_id,
                    bank_transaction_id=request.bank_transaction_id,
                    action_type=None,
                    status="rejected",
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_payload_hash,
                    calculation_payload=None,
                    calculation_hash=None,
                    error_code=code,
                    error_field_path=field_path,
                    error_count=1,
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                self._assert_late_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.REJECTED,
                    action_id=action.id,
                    errors=[BankStatementIssue(code=code, field_path=field_path)],
                )
        except IntegrityError:
            return self._late_concurrency_result(request, request_payload_hash)
        except DBAPIError as exc:
            return self._action_error(self._late_database_error_code(exc))

    def _replay_late_action(
        self,
        action: LateBankEvidenceAction,
    ) -> BankStatementActionResult:
        return BankStatementActionResult(
            status=(
                BankStatementActionStatus.POSTED
                if action.status == "posted"
                else BankStatementActionStatus.REJECTED
            ),
            action_id=action.id,
            calculation_hash=action.calculation_hash,
            errors=(
                [
                    BankStatementIssue(
                        code=action.error_code or "LATE_BANK_EVIDENCE_REJECTED",
                        field_path=action.error_field_path,
                    )
                ]
                if action.status == "rejected"
                else []
            ),
            trace=[{"stage": "late_bank_evidence_idempotent_replay"}],
            data={"idempotent_replay": True},
        )

    def _late_concurrency_result(
        self,
        request: ConfirmLateBankEvidenceRequest,
        request_payload_hash: str,
    ) -> BankStatementActionResult:
        winner = self.session.scalar(
            select(LateBankEvidenceAction).where(
                LateBankEvidenceAction.org_id == request.org_id,
                LateBankEvidenceAction.idempotency_key == request.idempotency_key,
            )
        )
        if winner is None:
            return self._action_error("LATE_BANK_EVIDENCE_CONCURRENT_WRITE_CONFLICT")
        if winner.request_payload_hash != request_payload_hash:
            return self._action_error("LATE_BANK_EVIDENCE_IDEMPOTENCY_PAYLOAD_MISMATCH")
        return self._replay_late_action(winner)

    @staticmethod
    def _reconciliation_issues(
        preview: BankReconciliationPreview,
    ) -> list[BankStatementIssue]:
        issues = list(preview.errors)
        for requirement in preview.missing_information:
            fields = requirement.fields or [None]
            issues.extend(
                BankStatementIssue(code=requirement.code, field_path=field)
                for field in fields
            )
        return issues or [BankStatementIssue(code="BANK_RECONCILIATION_REJECTED")]

    def _persist_reconciliation_rejection(
        self,
        request: ConfirmBankReconciliationRequest,
        request_payload_hash: str,
        issues: list[BankStatementIssue],
        attribution_id: uuid.UUID,
    ) -> BankStatementActionResult:
        issues = issues or [BankStatementIssue(code="BANK_RECONCILIATION_REJECTED")]
        codes = {item.code for item in issues}
        if "BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED" in codes:
            return BankStatementActionResult(
                status=BankStatementActionStatus.NEEDS_INFORMATION,
                missing_information=[
                    BankStatementInformationRequirement(
                        code="BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                        fields=["bank_reconciliation_scope"],
                    )
                ],
            )
        period_exists = self.session.scalar(
            select(AccountingPeriod.id).where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.id == request.period_id,
            )
        )
        account_exists = self.session.scalar(
            select(Account.id).where(
                Account.org_id == request.org_id,
                Account.code == request.bank_account_code,
            )
        )
        if period_exists is None or account_exists is None:
            return BankStatementActionResult(
                status=BankStatementActionStatus.REJECTED,
                errors=issues,
            )
        try:
            with self.session.begin_nested():
                action = BankReconciliationAction(
                    org_id=request.org_id,
                    period_id=request.period_id,
                    bank_account_code=request.bank_account_code,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_payload_hash,
                    calculation_hash=None,
                    status="rejected",
                    error_count=len(issues),
                    execution_attribution_id=attribution_id,
                )
                self.session.add(action)
                self.session.flush()
                self.session.add_all(
                    [
                        BankReconciliationFailure(
                            org_id=request.org_id,
                            action_id=action.id,
                            error_ordinal=index,
                            code=issue.code,
                            field_path=issue.field_path,
                        )
                        for index, issue in enumerate(issues, start=1)
                    ]
                )
                self.session.flush()
                self._assert_reconciliation_constraints_now()
                return BankStatementActionResult(
                    status=BankStatementActionStatus.REJECTED,
                    action_id=action.id,
                    errors=issues,
                    trace=[{"stage": "bank_reconciliation_confirmation_rejected"}],
                )
        except IntegrityError:
            return self._reconciliation_concurrency_result(request, request_payload_hash)
        except DBAPIError as exc:
            return self._action_error(self._reconciliation_database_error_code(exc))

    def _replay_reconciliation_action(
        self,
        action: BankReconciliationAction,
    ) -> BankStatementActionResult:
        failures = self.session.scalars(
            select(BankReconciliationFailure)
            .where(
                BankReconciliationFailure.org_id == action.org_id,
                BankReconciliationFailure.action_id == action.id,
            )
            .order_by(BankReconciliationFailure.error_ordinal)
        ).all()
        reconciliation = self.session.scalar(
            select(BankReconciliation).where(
                BankReconciliation.org_id == action.org_id,
                BankReconciliation.action_id == action.id,
            )
        )
        return BankStatementActionResult(
            status=(
                BankStatementActionStatus.POSTED
                if action.status == "posted"
                else BankStatementActionStatus.REJECTED
            ),
            action_id=action.id,
            calculation_hash=action.calculation_hash,
            errors=[
                BankStatementIssue(code=item.code, field_path=item.field_path)
                for item in failures
            ],
            trace=[{"stage": "bank_reconciliation_idempotent_replay"}],
            data={
                "idempotent_replay": True,
                "reconciliation_id": (
                    str(reconciliation.id) if reconciliation is not None else None
                ),
                "version": reconciliation.version if reconciliation is not None else None,
            },
        )

    def _add_reconciliation_edges(
        self,
        reconciliation: BankReconciliation,
        facts: BankReconciliationSystemFacts,
    ) -> None:
        self.session.add_all(
            [
                BankReconciliationEvidence(
                    org_id=reconciliation.org_id,
                    reconciliation_id=reconciliation.id,
                    evidence_id=item.evidence_id,
                    evidence_sha256_at_confirm=item.sha256,
                )
                for item in facts.statement_evidence
            ]
        )
        self.session.add_all(
            [
                BankReconciliationImportAction(
                    org_id=reconciliation.org_id,
                    reconciliation_id=reconciliation.id,
                    import_action_id=item.action_id,
                    request_payload_hash_at_confirm=item.request_payload_hash,
                    calculation_hash_at_confirm=item.calculation_hash,
                )
                for item in facts.import_actions
            ]
        )
        self.session.add_all(
            [
                BankReconciliationTransaction(
                    org_id=reconciliation.org_id,
                    reconciliation_id=reconciliation.id,
                    bank_transaction_id=transaction.transaction_id,
                    booking_date_at_confirm=transaction.booking_date,
                    amount_fen_at_confirm=transaction.amount_fen,
                )
                for action in facts.import_actions
                for transaction in action.transactions
            ]
        )

    def _reconciliation_concurrency_result(
        self,
        request: ConfirmBankReconciliationRequest,
        request_payload_hash: str,
    ) -> BankStatementActionResult:
        winner = self.session.scalar(
            select(BankReconciliationAction).where(
                BankReconciliationAction.org_id == request.org_id,
                BankReconciliationAction.idempotency_key == request.idempotency_key,
            )
        )
        if winner is None:
            return self._action_error("BANK_RECONCILIATION_CONCURRENT_WRITE_CONFLICT")
        if winner.request_payload_hash != request_payload_hash:
            return self._action_error(
                "BANK_RECONCILIATION_IDEMPOTENCY_PAYLOAD_MISMATCH"
            )
        return self._replay_reconciliation_action(winner)

    @staticmethod
    def _preview_issues(preview: BankStatementImportPreview) -> list[BankStatementIssue]:
        issues = list(preview.errors)
        for requirement in preview.missing_information:
            fields = requirement.fields or [None]
            issues.extend(
                BankStatementIssue(code=requirement.code, field_path=field) for field in fields
            )
        return issues

    @staticmethod
    def _action_from_preview(
        preview: BankStatementImportPreview,
    ) -> BankStatementActionResult:
        return BankStatementActionResult(
            status=(
                BankStatementActionStatus.NEEDS_INFORMATION
                if preview.status == BankStatementPreviewStatus.NEEDS_INFORMATION
                else BankStatementActionStatus.REJECTED
            ),
            missing_information=preview.missing_information,
            errors=preview.errors,
            trace=preview.trace,
        )

    @staticmethod
    def _action_error(
        code: str,
        *,
        field_path: str | None = None,
    ) -> BankStatementActionResult:
        return BankStatementActionResult(
            status=BankStatementActionStatus.REJECTED,
            errors=[BankStatementIssue(code=code, field_path=field_path)],
        )

    def _database_clock(self) -> datetime:
        if self.session.get_bind().dialect.name == "postgresql":
            value = self.session.scalar(text("SELECT clock_timestamp()"))
            if isinstance(value, datetime):
                return self._aware(value)
        return datetime.now(UTC)

    def _assert_import_constraints_now(self) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text("SELECT to_regclass('public.bank_statement_import_actions') IS NOT NULL")
        )
        if installed is not True:
            return
        names = (
            "bank_import_action_invariant_deferred_0015, "
            "bank_import_failure_invariant_deferred_0015, "
            "bank_import_evidence_invariant_deferred_0015, "
            "bank_transaction_import_invariant_deferred_0015"
        )
        self.session.execute(text(f"SET CONSTRAINTS {names} IMMEDIATE"))
        self.session.execute(text(f"SET CONSTRAINTS {names} DEFERRED"))

    def _assert_scope_constraints_now(self) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text(
                "SELECT to_regclass('public.bank_reconciliation_scope_actions') "
                "IS NOT NULL"
            )
        )
        if installed is not True:
            return
        names = (
            "bank_scope_action_invariant_deferred_0015, "
            "bank_scope_action_evidence_invariant_deferred_0015"
        )
        self.session.execute(text(f"SET CONSTRAINTS {names} IMMEDIATE"))
        self.session.execute(text(f"SET CONSTRAINTS {names} DEFERRED"))

    def _assert_late_constraints_now(self) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text("SELECT to_regclass('public.late_bank_evidence_actions') IS NOT NULL")
        )
        if installed is not True:
            return
        names = (
            "late_bank_action_invariant_deferred_0015, "
            "late_bank_action_evidence_invariant_deferred_0015"
        )
        self.session.execute(text(f"SET CONSTRAINTS {names} IMMEDIATE"))
        self.session.execute(text(f"SET CONSTRAINTS {names} DEFERRED"))

    def _assert_reconciliation_constraints_now(self) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        installed = self.session.scalar(
            text("SELECT to_regclass('public.bank_reconciliations') IS NOT NULL")
        )
        if installed is not True:
            return
        names = (
            "bank_reconciliation_action_invariant_deferred_0015, "
            "bank_reconciliation_failure_invariant_deferred_0015, "
            "bank_reconciliation_invariant_deferred_0015, "
            "bank_reconciliation_evidence_invariant_deferred_0015, "
            "bank_reconciliation_import_invariant_deferred_0015, "
            "bank_reconciliation_transaction_invariant_deferred_0015"
        )
        self.session.execute(text(f"SET CONSTRAINTS {names} IMMEDIATE"))
        self.session.execute(text(f"SET CONSTRAINTS {names} DEFERRED"))

    def _lock_tax_period_org(self, org_id: uuid.UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('tax-period-org:' || :org_id, 0))"
                ),
                {"org_id": str(org_id)},
            )

    def _lock_month(self, org_id: uuid.UUID, month: date) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended("
                    "'accounting_period:' || CAST(:org_id AS text) || ':' || "
                    "CAST(date_trunc('month', CAST(:month AS date))::date AS text), 0))"
                ),
                {"org_id": str(org_id), "month": month},
            )

    @staticmethod
    def _bank_database_error_code(exc: DBAPIError) -> str:
        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        message = getattr(diagnostics, "message_primary", None)
        if isinstance(message, str) and message.startswith("BANK_STATEMENT_"):
            return message
        return "BANK_STATEMENT_DATABASE_OPERATION_FAILED"

    @staticmethod
    def _scope_database_error_code(exc: DBAPIError) -> str:
        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        message = getattr(diagnostics, "message_primary", None)
        if isinstance(message, str) and message.startswith("BANK_RECONCILIATION_SCOPE_"):
            return message
        return "BANK_RECONCILIATION_SCOPE_DATABASE_OPERATION_FAILED"

    @staticmethod
    def _late_database_error_code(exc: DBAPIError) -> str:
        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        message = getattr(diagnostics, "message_primary", None)
        if isinstance(message, str) and message.startswith("LATE_BANK_EVIDENCE_"):
            return message
        return "LATE_BANK_EVIDENCE_DATABASE_OPERATION_FAILED"

    @staticmethod
    def _reconciliation_database_error_code(exc: DBAPIError) -> str:
        original = getattr(exc, "orig", None)
        diagnostics = getattr(original, "diag", None)
        message = getattr(diagnostics, "message_primary", None)
        if isinstance(message, str) and message.startswith("BANK_RECONCILIATION_"):
            return message
        return "BANK_RECONCILIATION_DATABASE_OPERATION_FAILED"

    def _period_projection(
        self,
        booking_date: date,
        period: AccountingPeriod | None,
    ) -> BankStatementPeriodProjection:
        start = booking_date.replace(day=1)
        end = booking_date.replace(day=monthrange(booking_date.year, booking_date.month)[1])
        if period is None:
            return BankStatementPeriodProjection(
                status="not_generated",
                period_start_date=start,
                period_end_date=end,
            )
        if period.status == "open":
            return BankStatementPeriodProjection(
                status="open",
                period_id=period.id,
                period_start_date=period.start_date,
                period_end_date=period.end_date,
            )
        close = self.session.scalar(
            select(AccountingPeriodClose).where(
                AccountingPeriodClose.org_id == period.org_id,
                AccountingPeriodClose.id == period.close_id,
            )
        )
        if close is None or period.closed_at is None:
            return BankStatementPeriodProjection(
                status="not_generated",
                period_start_date=start,
                period_end_date=end,
            )
        return BankStatementPeriodProjection(
            status="closed",
            period_id=period.id,
            close_id=close.id,
            close_hash=close.calculation_hash,
            closed_at=self._aware(period.closed_at),
            period_start_date=period.start_date,
            period_end_date=period.end_date,
        )

    @staticmethod
    def _snapshot(row: BankTransaction | None) -> BankStatementTransactionSnapshot | None:
        if row is None:
            return None
        return BankStatementTransactionSnapshot(
            transaction_id=row.id,
            org_id=row.org_id,
            bank_account_code=row.bank_account_code,
            external_id=row.external_id,
            booking_date=row.booking_date,
            amount_fen=row.amount_fen,
            currency=row.currency,
            counterparty_name=row.counterparty_name,
            memo=row.memo,
            fingerprint=row.fingerprint,
            source_sha256=row.source_sha256,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    def _today(self) -> date:
        return self._current_date or china_current_date()

    @staticmethod
    def _safe_path_error_code(exc: PathSecurityError) -> str:
        code = str(exc)
        if code == "FILE_TOO_LARGE":
            return "BANK_STATEMENT_FILE_LIMIT_EXCEEDED"
        if code == "FILE_FORMAT_NOT_ALLOWED":
            return "BANK_STATEMENT_FORMAT_UNSUPPORTED"
        return "BANK_STATEMENT_INPUT_UNAVAILABLE"

    @staticmethod
    def _rejected_import_preview(
        code: str,
        *,
        field_path: str | None = None,
    ) -> BankStatementImportPreview:
        return BankStatementImportPreview(
            status=BankStatementPreviewStatus.REJECTED,
            errors=[BankStatementIssue(code=code, field_path=field_path)],
            trace=[{"stage": "bank_statement_preview_rejected", "code": code}],
        )
