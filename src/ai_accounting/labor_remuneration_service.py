"""Controlled non-employee personal labor-remuneration workflow.

This module intentionally owns its tax calculation and posting templates.  Its
public requests contain business facts only; no account, debit, credit, rate,
quick-deduction, or free-form journal input is accepted.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .labor_remuneration_schemas import (
    ConfirmLaborExternalDeclarationRequest,
    ConfirmLaborRemunerationBatchRequest,
    ConfirmUnifiedPayoutRunRequest,
    EndLaborServicePersonRequest,
    GetLaborRemunerationRequest,
    LaborInformationRequirement,
    LaborResult,
    LaborResultStatus,
    PayLaborWithholdingTaxRequest,
    PreviewLaborRemunerationBatchRequest,
    PreviewUnifiedPayoutRunRequest,
    RegisterLaborServicePersonRequest,
)
from .ledger import (
    AccountingPeriodError,
    Entry,
    OpenItemPlan,
    assert_period_open,
    create_open_items,
    create_voucher,
)
from .models import (
    AuditLog,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Employee,
    Evidence,
    LaborExternalDeclarationConfirmation,
    LaborExternalDeclarationEvidence,
    LaborRemunerationBatch,
    LaborRemunerationBatchEvidence,
    LaborRemunerationEventLink,
    LaborRemunerationLine,
    LaborRemunerationTaxPolicyVersion,
    LaborServicePerson,
    LaborServicePersonEndAction,
    LaborServicePersonEndActionEvidence,
    LaborServicePersonEvidence,
    LaborWithholdingEntitlement,
    LaborWithholdingOpenItemSource,
    LaborWithholdingTaxPaymentAllocation,
    OpenItem,
    Organization,
    PayrollEventLink,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    UnifiedPayoutRun,
    UnifiedPayoutRunEvidence,
    UnifiedPayoutRunItem,
    Voucher,
)
from .schemas import RecordEventRequest, ResultStatus, ReverseEventRequest
from .service import FinanceService

POLICY_CODE = "cn_resident_labor_remuneration_withholding"
RULE_VERSION_PREFIX = "labor-remuneration/"


def _round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_resident_labor_withholding(
    gross_fen: int, parameters: dict[str, Any]
) -> dict[str, Any]:
    """Apply the effective ordinary-resident labor withholding policy in fen."""

    if isinstance(gross_fen, bool) or not isinstance(gross_fen, int) or gross_fen <= 0:
        raise ValueError("LABOR_GROSS_REMUNERATION_MUST_BE_POSITIVE_INTEGER_FEN")
    threshold = int(parameters["small_payment_threshold_fen"])
    fixed_deduction = int(parameters["fixed_expense_deduction_fen"])
    if gross_fen <= threshold:
        taxable_fen = max(gross_fen - fixed_deduction, 0)
        expense_deduction_fen = gross_fen - taxable_fen
        deduction_method = "fixed_800_yuan"
    else:
        expense_rate = Decimal(str(parameters["large_payment_expense_rate"]))
        taxable_fen = _round_fen(Decimal(gross_fen) * (Decimal("1") - expense_rate))
        expense_deduction_fen = gross_fen - taxable_fen
        deduction_method = "twenty_percent"

    selected: dict[str, Any] | None = None
    for bracket in parameters["withholding_brackets"]:
        upper = bracket.get("upper_taxable_income_fen")
        if upper is None or taxable_fen <= int(upper):
            selected = bracket
            break
    if selected is None:
        raise ValueError("LABOR_TAX_POLICY_HAS_NO_APPLICABLE_BRACKET")
    rate = Decimal(str(selected["rate"]))
    quick_deduction_fen = int(selected["quick_deduction_fen"])
    withholding_tax_fen = max(
        _round_fen(Decimal(taxable_fen) * rate) - quick_deduction_fen,
        0,
    )
    return {
        "gross_remuneration_fen": gross_fen,
        "expense_deduction_fen": expense_deduction_fen,
        "taxable_income_fen": taxable_fen,
        "withholding_rate": str(rate),
        "quick_deduction_fen": quick_deduction_fen,
        "withholding_tax_fen": withholding_tax_fen,
        "net_payment_fen": gross_fen - withholding_tax_fen,
        "trace": [
            {
                "stage": "ordinary_resident_expense_deduction",
                "method": deduction_method,
                "expense_deduction_fen": expense_deduction_fen,
                "taxable_income_fen": taxable_fen,
            },
            {
                "stage": "ordinary_resident_withholding",
                "rate": str(rate),
                "quick_deduction_fen": quick_deduction_fen,
                "rounding": "half_up_to_fen",
                "withholding_tax_fen": withholding_tax_fen,
            },
        ],
    }


class LaborRemunerationService:
    def __init__(self, session: Session):
        self.session = session
        self.finance = FinanceService(session)

    @staticmethod
    def _hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _requirement(fields: list[str]) -> LaborResult:
        return LaborResult(
            status=LaborResultStatus.NEEDS_INFORMATION,
            missing_information=[
                LaborInformationRequirement(
                    code="LABOR_REMUNERATION_BUSINESS_FACTS_REQUIRED",
                    fields=fields,
                    message="缺少会改变个人劳务报酬税务或会计处理的明确业务事实",
                )
            ],
        )

    @staticmethod
    def _rejected(*errors: str) -> LaborResult:
        return LaborResult(status=LaborResultStatus.REJECTED, errors=list(errors))

    def _organization(self, org_id: uuid.UUID) -> Organization:
        organization = self.session.get(Organization, org_id)
        if organization is None:
            raise ValueError("ORGANIZATION_NOT_FOUND")
        return organization

    def _evidence(self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]) -> list[Evidence]:
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("DUPLICATE_LABOR_EVIDENCE_REFERENCE")
        evidence = self.session.scalars(
            select(Evidence).where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
        ).all()
        if len(evidence) != len(evidence_ids):
            raise ValueError("LABOR_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
        return evidence

    @staticmethod
    def _policy_snapshot(policy: LaborRemunerationTaxPolicyVersion) -> dict[str, Any]:
        return {
            "id": str(policy.id),
            "code": policy.code,
            "version": policy.version,
            "effective_from": policy.effective_from.isoformat(),
            "effective_to": policy.effective_to.isoformat() if policy.effective_to else None,
            "primary_source_url": policy.primary_source_url,
            "invoice_withholding_source_url": policy.invoice_withholding_source_url,
            "legal_filing_source_url": policy.legal_filing_source_url,
            "parameters": policy.parameters,
        }

    def _active_policy(self, on_date: date) -> LaborRemunerationTaxPolicyVersion:
        policies = self.session.scalars(
            select(LaborRemunerationTaxPolicyVersion)
            .where(
                LaborRemunerationTaxPolicyVersion.code == POLICY_CODE,
                LaborRemunerationTaxPolicyVersion.effective_from <= on_date,
                (
                    LaborRemunerationTaxPolicyVersion.effective_to.is_(None)
                    | (LaborRemunerationTaxPolicyVersion.effective_to >= on_date)
                ),
            )
            .order_by(LaborRemunerationTaxPolicyVersion.effective_from.desc())
        ).all()
        if len(policies) != 1:
            raise ValueError("LABOR_TAX_POLICY_NOT_UNIQUELY_EFFECTIVE")
        return policies[0]

    def _result_for_person(
        self, person: LaborServicePerson, *, replay: bool = False
    ) -> LaborResult:
        linked_employee_id = self.session.scalar(
            select(Employee.id).where(
                Employee.org_id == person.org_id,
                Employee.prior_labor_person_id == person.id,
            )
        )
        return LaborResult(
            status=LaborResultStatus.REGISTERED,
            labor_person_id=person.id,
            data={
                "idempotent_replay": replay,
                "counterparty_id": str(person.counterparty_id),
                "person_code": person.person_code,
                "name": person.name,
                "relationship_start_date": person.relationship_start_date.isoformat(),
                "relationship_end_date": (
                    person.relationship_end_date.isoformat()
                    if person.relationship_end_date
                    else None
                ),
                "status": person.status,
                "linked_employee_id": (
                    str(linked_employee_id) if linked_employee_id is not None else None
                ),
            },
        )

    def register_person(self, request: RegisterLaborServicePersonRequest) -> LaborResult:
        missing = request.missing_fields()
        if missing:
            return self._requirement(missing)
        payload_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                self._organization(request.org_id)
                existing = self.session.scalar(
                    select(LaborServicePerson).where(
                        LaborServicePerson.org_id == request.org_id,
                        LaborServicePerson.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != payload_hash:
                        return self._rejected("LABOR_PERSON_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    return self._result_for_person(existing, replay=True)
                self._evidence(request.org_id, request.evidence_references)
                code_conflict = self.session.scalar(
                    select(LaborServicePerson.id).where(
                        LaborServicePerson.org_id == request.org_id,
                        LaborServicePerson.person_code == request.person_code,
                    )
                )
                if code_conflict is not None:
                    return self._rejected("LABOR_PERSON_CODE_ALREADY_EXISTS")
                counterparty = self.session.scalar(
                    select(Counterparty).where(
                        Counterparty.org_id == request.org_id,
                        Counterparty.kind == "labor_person",
                        Counterparty.name == request.name,
                    )
                )
                if counterparty is not None:
                    return self._rejected("LABOR_PERSON_IDENTITY_ALREADY_EXISTS")
                counterparty = Counterparty(
                    org_id=request.org_id,
                    kind="labor_person",
                    name=request.name,
                    external_ref=request.person_code,
                )
                self.session.add(counterparty)
                self.session.flush()
                if self.session.scalar(
                    select(Employee.id).where(Employee.counterparty_id == counterparty.id)
                ):
                    return self._rejected("LABOR_PERSON_MUST_NOT_BE_AN_EMPLOYEE")
                person = LaborServicePerson(
                    org_id=request.org_id,
                    counterparty_id=counterparty.id,
                    person_code=request.person_code,
                    name=request.name,
                    relationship_start_date=request.relationship_start_date,
                    relationship_end_date=request.relationship_end_date,
                    status=request.status,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=payload_hash,
                )
                self.session.add(person)
                self.session.flush()
                for evidence_id in request.evidence_references:
                    self.session.add(
                        LaborServicePersonEvidence(
                            org_id=request.org_id,
                            labor_person_id=person.id,
                            evidence_id=evidence_id,
                        )
                    )
                if person.status == "ended":
                    end_action = LaborServicePersonEndAction(
                        org_id=request.org_id,
                        labor_person_id=person.id,
                        relationship_end_date=person.relationship_end_date,
                        idempotency_key=request.idempotency_key,
                        request_payload_hash=payload_hash,
                    )
                    self.session.add(end_action)
                    self.session.flush()
                    for evidence_id in request.evidence_references:
                        self.session.add(
                            LaborServicePersonEndActionEvidence(
                                org_id=request.org_id,
                                action_id=end_action.id,
                                evidence_id=evidence_id,
                            )
                        )
                self.session.flush()
                return self._result_for_person(person)
        except (IntegrityError, OperationalError):
            existing = self.session.scalar(
                select(LaborServicePerson).where(
                    LaborServicePerson.org_id == request.org_id,
                    LaborServicePerson.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None and existing.request_payload_hash == payload_hash:
                return self._result_for_person(existing, replay=True)
            return self._rejected("LABOR_PERSON_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def end_person(self, request: EndLaborServicePersonRequest) -> LaborResult:
        payload_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                self._organization(request.org_id)
                existing = self.session.scalar(
                    select(LaborServicePersonEndAction).where(
                        LaborServicePersonEndAction.org_id == request.org_id,
                        LaborServicePersonEndAction.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != payload_hash:
                        return self._rejected("LABOR_PERSON_END_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    person = self.session.get(LaborServicePerson, existing.labor_person_id)
                    if person is None:
                        return self._rejected("LABOR_PERSON_END_ACTION_SCOPE_CONFLICT")
                    return self._result_for_person(person, replay=True)
                person = self.session.scalar(
                    select(LaborServicePerson)
                    .where(
                        LaborServicePerson.org_id == request.org_id,
                        LaborServicePerson.id == request.labor_person_id,
                    )
                    .with_for_update()
                )
                if person is None:
                    return self._rejected("LABOR_PERSON_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
                if person.status != "active" or person.relationship_end_date is not None:
                    return self._rejected("LABOR_PERSON_RELATIONSHIP_ALREADY_ENDED")
                if request.relationship_end_date < person.relationship_start_date:
                    return self._rejected("LABOR_PERSON_END_DATE_PRECEDES_START_DATE")
                if self.session.scalar(
                    select(LaborRemunerationLine.id).where(
                        LaborRemunerationLine.org_id == request.org_id,
                        LaborRemunerationLine.labor_person_id == person.id,
                        LaborRemunerationLine.service_end_date > request.relationship_end_date,
                    )
                ):
                    return self._rejected("LABOR_PERSON_END_DATE_PRECEDES_RECORDED_SERVICE")
                self._evidence(request.org_id, request.evidence_references)
                action = LaborServicePersonEndAction(
                    org_id=request.org_id,
                    labor_person_id=person.id,
                    relationship_end_date=request.relationship_end_date,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=payload_hash,
                )
                self.session.add(action)
                self.session.flush()
                for evidence_id in request.evidence_references:
                    self.session.add(
                        LaborServicePersonEndActionEvidence(
                            org_id=request.org_id,
                            action_id=action.id,
                            evidence_id=evidence_id,
                        )
                    )
                person.relationship_end_date = request.relationship_end_date
                person.status = "ended"
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        action="labor_service_person_relationship_ended",
                        details={
                            "labor_person_id": str(person.id),
                            "end_action_id": str(action.id),
                            "relationship_end_date": request.relationship_end_date.isoformat(),
                        },
                    )
                )
                self.session.flush()
                result = self._result_for_person(person)
                result.data["end_action_id"] = str(action.id)
                return result
        except (IntegrityError, OperationalError):
            existing = self.session.scalar(
                select(LaborServicePersonEndAction).where(
                    LaborServicePersonEndAction.org_id == request.org_id,
                    LaborServicePersonEndAction.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None and existing.request_payload_hash == payload_hash:
                person = self.session.get(LaborServicePerson, existing.labor_person_id)
                if person is not None:
                    return self._result_for_person(person, replay=True)
            return self._rejected("LABOR_PERSON_END_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def _derive_batch(
        self, request: PreviewLaborRemunerationBatchRequest
    ) -> tuple[LaborRemunerationTaxPolicyVersion, dict[str, Any], list[dict[str, Any]]]:
        assert request.planned_payment_date is not None
        policy = self._active_policy(request.planned_payment_date)
        policy_snapshot = self._policy_snapshot(policy)
        self._evidence(request.org_id, request.evidence_references)
        derived_lines: list[dict[str, Any]] = []
        seen_people: set[uuid.UUID] = set()
        for index, item in enumerate(request.items):
            assert item.labor_person_id is not None
            if item.labor_person_id in seen_people:
                raise ValueError("ONE_LABOR_PERSON_MAY_APPEAR_ONLY_ONCE_PER_BATCH")
            seen_people.add(item.labor_person_id)
            person = self.session.scalar(
                select(LaborServicePerson).where(
                    LaborServicePerson.org_id == request.org_id,
                    LaborServicePerson.id == item.labor_person_id,
                )
            )
            if person is None:
                raise ValueError("LABOR_PERSON_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
            if self.session.scalar(
                select(Employee.id).where(
                    Employee.org_id == request.org_id,
                    Employee.counterparty_id == person.counterparty_id,
                )
            ):
                raise ValueError("LABOR_PERSON_MUST_NOT_BE_AN_EMPLOYEE")
            assert item.service_start_date is not None and item.service_end_date is not None
            if item.service_start_date < person.relationship_start_date:
                raise ValueError("LABOR_SERVICE_OUTSIDE_RELATIONSHIP_PERIOD")
            if (
                person.relationship_end_date is not None
                and item.service_end_date > person.relationship_end_date
            ):
                raise ValueError("LABOR_SERVICE_OUTSIDE_RELATIONSHIP_PERIOD")
            if item.tax_identity == "nonresident":
                raise ValueError("NONRESIDENT_LABOR_REMUNERATION_NOT_SUPPORTED")
            if item.is_full_time_student is True:
                raise ValueError("STUDENT_INTERNSHIP_WITHHOLDING_METHOD_NOT_SUPPORTED")
            if item.tax_identity != "resident" or item.is_full_time_student is not False:
                raise ValueError("LABOR_TAX_IDENTITY_IS_NOT_SUPPORTED")
            if item.external_declaration_status == "confirmed":
                raise ValueError("LABOR_EXTERNAL_DECLARATION_MUST_USE_CONFIRMATION_ACTION")
            assert item.fixed_fee_fen is not None and item.commission_fen is not None
            gross = item.fixed_fee_fen + item.commission_fen
            calculation = calculate_resident_labor_withholding(gross, policy.parameters)
            derived_lines.append(
                {
                    "index": index,
                    "labor_person_id": str(person.id),
                    "counterparty_id": str(person.counterparty_id),
                    "service_start_date": item.service_start_date.isoformat(),
                    "service_end_date": item.service_end_date.isoformat(),
                    "fixed_fee_fen": item.fixed_fee_fen,
                    "commission_fen": item.commission_fen,
                    "expense_role": item.expense_role,
                    "tax_identity": item.tax_identity,
                    "income_grouping": item.income_grouping,
                    "is_full_time_student": False,
                    "external_declaration_status": item.external_declaration_status,
                    "external_declaration_reference": item.external_declaration_reference,
                    **{key: value for key, value in calculation.items() if key != "trace"},
                    "calculation_trace": calculation["trace"],
                }
            )
        calculation_input = {
            "request": request.model_dump(mode="json"),
            "policy": policy_snapshot,
            "lines": derived_lines,
        }
        return policy, calculation_input, derived_lines

    def preview_batch(self, request: PreviewLaborRemunerationBatchRequest) -> LaborResult:
        missing = request.missing_fields()
        if missing:
            return self._requirement(missing)
        payload_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                self._organization(request.org_id)
                assert request.posting_date is not None
                assert_period_open(self.session, request.org_id, request.posting_date)
                existing = self.session.scalar(
                    select(LaborRemunerationBatch).where(
                        LaborRemunerationBatch.org_id == request.org_id,
                        LaborRemunerationBatch.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != payload_hash:
                        return self._rejected("LABOR_BATCH_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    return self._batch_result(existing, replay=True)
                policy, calculation_input, lines = self._derive_batch(request)
                calculation_hash = self._hash(calculation_input)
                batch = LaborRemunerationBatch(
                    org_id=request.org_id,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=payload_hash,
                    remuneration_period=request.remuneration_period,
                    status="calculated",
                    calculation_hash=calculation_hash,
                    calculation_input=calculation_input,
                    calculation_trace=[
                        {
                            "stage": "policy_selected",
                            "code": policy.code,
                            "version": policy.version,
                            "source_url": policy.primary_source_url,
                        },
                        {
                            "stage": "batch_totals",
                            "gross_fen": sum(line["gross_remuneration_fen"] for line in lines),
                            "withholding_tax_fen": sum(
                                line["withholding_tax_fen"] for line in lines
                            ),
                            "net_fen": sum(line["net_payment_fen"] for line in lines),
                        },
                    ],
                    policy_version_id=policy.id,
                    policy_snapshot=self._policy_snapshot(policy),
                    business_date=request.business_date,
                    posting_date=request.posting_date,
                    planned_payment_date=request.planned_payment_date,
                )
                self.session.add(batch)
                self.session.flush()
                for values in lines:
                    line_values = dict(values)
                    line_values.pop("index")
                    line_values["labor_person_id"] = uuid.UUID(line_values["labor_person_id"])
                    line_values["counterparty_id"] = uuid.UUID(line_values["counterparty_id"])
                    line_values["service_start_date"] = date.fromisoformat(
                        line_values["service_start_date"]
                    )
                    line_values["service_end_date"] = date.fromisoformat(
                        line_values["service_end_date"]
                    )
                    line_values["withholding_rate"] = Decimal(line_values["withholding_rate"])
                    self.session.add(
                        LaborRemunerationLine(
                            org_id=request.org_id,
                            batch_id=batch.id,
                            **line_values,
                        )
                    )
                for evidence_id in request.evidence_references:
                    self.session.add(
                        LaborRemunerationBatchEvidence(
                            org_id=request.org_id,
                            batch_id=batch.id,
                            evidence_id=evidence_id,
                        )
                    )
                self.session.flush()
                return self._batch_result(batch)
        except AccountingPeriodError as exc:
            return self._rejected(exc.code)
        except (IntegrityError, OperationalError):
            existing = self.session.scalar(
                select(LaborRemunerationBatch).where(
                    LaborRemunerationBatch.org_id == request.org_id,
                    LaborRemunerationBatch.idempotency_key == request.idempotency_key,
                )
            )
            if existing is not None and existing.request_payload_hash == payload_hash:
                return self._batch_result(existing, replay=True)
            return self._rejected("LABOR_BATCH_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def _batch_result(self, batch: LaborRemunerationBatch, *, replay: bool = False) -> LaborResult:
        lines = self.session.scalars(
            select(LaborRemunerationLine)
            .where(
                LaborRemunerationLine.org_id == batch.org_id,
                LaborRemunerationLine.batch_id == batch.id,
            )
            .order_by(LaborRemunerationLine.id)
        ).all()
        declarations = {
            item.labor_line_id: item
            for item in self.session.scalars(
                select(LaborExternalDeclarationConfirmation).where(
                    LaborExternalDeclarationConfirmation.org_id == batch.org_id,
                    LaborExternalDeclarationConfirmation.labor_line_id.in_(
                        [line.id for line in lines]
                    ),
                )
            ).all()
        }
        status = LaborResultStatus(batch.status)
        voucher = self.session.scalar(
            select(Voucher).where(Voucher.event_id == batch.business_event_id)
        )
        return LaborResult(
            status=status,
            batch_id=batch.id,
            event_id=batch.business_event_id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            calculation_hash=batch.calculation_hash,
            trace=batch.calculation_trace,
            data={
                "idempotent_replay": replay,
                "remuneration_period": batch.remuneration_period,
                "business_date": batch.business_date.isoformat(),
                "posting_date": batch.posting_date.isoformat(),
                "planned_payment_date": batch.planned_payment_date.isoformat(),
                "policy_snapshot": batch.policy_snapshot,
                "totals": {
                    "fixed_fee_fen": sum(line.fixed_fee_fen for line in lines),
                    "commission_fen": sum(line.commission_fen for line in lines),
                    "gross_fen": sum(line.gross_remuneration_fen for line in lines),
                    "withholding_tax_fen": sum(line.withholding_tax_fen for line in lines),
                    "net_fen": sum(line.net_payment_fen for line in lines),
                },
                "lines": [
                    {
                        "id": str(line.id),
                        "labor_person_id": str(line.labor_person_id),
                        "fixed_fee_fen": line.fixed_fee_fen,
                        "commission_fen": line.commission_fen,
                        "gross_fen": line.gross_remuneration_fen,
                        "expense_role": line.expense_role,
                        "tax_identity": line.tax_identity,
                        "income_grouping": line.income_grouping,
                        "is_full_time_student": line.is_full_time_student,
                        "service_start_date": line.service_start_date.isoformat(),
                        "service_end_date": line.service_end_date.isoformat(),
                        "expense_deduction_fen": line.expense_deduction_fen,
                        "taxable_income_fen": line.taxable_income_fen,
                        "withholding_rate": str(line.withholding_rate),
                        "quick_deduction_fen": line.quick_deduction_fen,
                        "withholding_tax_fen": line.withholding_tax_fen,
                        "net_fen": line.net_payment_fen,
                        "external_declaration_status": line.external_declaration_status,
                        "external_declaration_reference": (line.external_declaration_reference),
                        "current_external_declaration_status": (
                            "confirmed"
                            if line.id in declarations
                            else line.external_declaration_status
                        ),
                        "current_external_declaration_reference": (
                            declarations[line.id].external_declaration_reference
                            if line.id in declarations
                            else line.external_declaration_reference
                        ),
                    }
                    for line in lines
                ],
            },
        )

    def confirm_batch(self, request: ConfirmLaborRemunerationBatchRequest) -> LaborResult:
        request_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                existing_event = self.session.scalar(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.idempotency_key == request.idempotency_key,
                    )
                )
                if existing_event is not None:
                    if existing_event.request_payload_hash != request_hash:
                        return self._rejected("LABOR_CONFIRM_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    batch = self.session.scalar(
                        select(LaborRemunerationBatch).where(
                            LaborRemunerationBatch.business_event_id == existing_event.id
                        )
                    )
                    if batch is None:
                        return self._rejected("LABOR_CONFIRM_IDEMPOTENCY_SCOPE_CONFLICT")
                    return self._batch_result(batch, replay=True)
                batch = self.session.scalar(
                    select(LaborRemunerationBatch)
                    .where(
                        LaborRemunerationBatch.org_id == request.org_id,
                        LaborRemunerationBatch.id == request.batch_id,
                    )
                    .with_for_update()
                )
                if batch is None:
                    return self._rejected("LABOR_BATCH_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
                if batch.status != "calculated" or batch.business_event_id is not None:
                    winning_event = (
                        self.session.get(BusinessEvent, batch.business_event_id)
                        if batch.business_event_id is not None
                        else None
                    )
                    if (
                        winning_event is not None
                        and winning_event.idempotency_key == request.idempotency_key
                        and winning_event.request_payload_hash == request_hash
                    ):
                        return self._batch_result(batch, replay=True)
                    return self._rejected("LABOR_BATCH_IS_NOT_CONFIRMABLE")
                if request.calculation_hash != batch.calculation_hash:
                    return self._rejected("LABOR_CALCULATION_HASH_MISMATCH")
                if self._hash(batch.calculation_input) != batch.calculation_hash:
                    return self._rejected("LABOR_CALCULATION_SNAPSHOT_TAMPERED")
                assert_period_open(self.session, batch.org_id, batch.posting_date)
                lines = self.session.scalars(
                    select(LaborRemunerationLine)
                    .where(
                        LaborRemunerationLine.org_id == batch.org_id,
                        LaborRemunerationLine.batch_id == batch.id,
                    )
                    .order_by(LaborRemunerationLine.id)
                    .with_for_update()
                ).all()
                evidence_ids = self.session.scalars(
                    select(LaborRemunerationBatchEvidence.evidence_id).where(
                        LaborRemunerationBatchEvidence.org_id == batch.org_id,
                        LaborRemunerationBatchEvidence.batch_id == batch.id,
                    )
                ).all()
                event = BusinessEvent(
                    org_id=batch.org_id,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_hash,
                    event_type="labor_remuneration_accrual",
                    status="draft",
                    description=batch.calculation_input["request"]["description"],
                    facts={
                        "batch_id": str(batch.id),
                        "calculation_hash": batch.calculation_hash,
                        "remuneration_period": batch.remuneration_period,
                    },
                    business_date=batch.business_date,
                    posting_date=batch.posting_date,
                    rule_trace=batch.calculation_trace,
                    rule_version=f"{RULE_VERSION_PREFIX}{batch.policy_snapshot['version']}",
                )
                self.session.add(event)
                self.session.flush()
                self.session.add(
                    LaborRemunerationEventLink(
                        org_id=batch.org_id,
                        event_id=event.id,
                        batch_id=batch.id,
                        link_kind="accrual",
                    )
                )
                self.finance._attach_evidence(event, list(evidence_ids))
                entries: list[Entry] = []
                plans: list[OpenItemPlan] = []
                for line in lines:
                    entries.extend(
                        [
                            Entry(
                                account_role=line.expense_role,
                                debit_fen=line.gross_remuneration_fen,
                                counterparty_id=line.counterparty_id,
                            ),
                            Entry(
                                account_role="labor_remuneration_payable",
                                credit_fen=line.gross_remuneration_fen,
                                counterparty_id=line.counterparty_id,
                            ),
                        ]
                    )
                    plans.append(
                        OpenItemPlan(
                            counterparty_id=line.counterparty_id,
                            item_type="payable",
                            original_amount_fen=line.gross_remuneration_fen,
                            due_date=batch.planned_payment_date,
                            payable_category="labor_remuneration",
                        )
                    )
                    self.session.add(
                        LaborWithholdingEntitlement(
                            org_id=batch.org_id,
                            labor_line_id=line.id,
                            amount_fen=line.withholding_tax_fen,
                        )
                    )
                voucher = create_voucher(
                    self.session,
                    event=event,
                    posting_date=batch.posting_date,
                    description=event.description,
                    entries=entries,
                )
                create_open_items(self.session, event=event, plans=plans)
                batch.status = "posted"
                batch.business_event_id = event.id
                batch.confirmation_note = request.confirmation_note
                batch.confirmed_at = datetime.now(UTC)
                event.status = "posted"
                self.session.add(
                    AuditLog(
                        org_id=batch.org_id,
                        event_id=event.id,
                        action="labor_remuneration_confirmed",
                        details={
                            "batch_id": str(batch.id),
                            "calculation_hash": batch.calculation_hash,
                        },
                    )
                )
                self.session.flush()
                result = self._batch_result(batch)
                result.voucher_id = voucher.id
                result.voucher_number = voucher.voucher_number
                return result
        except AccountingPeriodError as exc:
            return self._rejected(exc.code)
        except (IntegrityError, OperationalError):
            winning_event = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if winning_event is not None and winning_event.request_payload_hash == request_hash:
                winning_batch = self.session.scalar(
                    select(LaborRemunerationBatch).where(
                        LaborRemunerationBatch.business_event_id == winning_event.id
                    )
                )
                if winning_batch is not None:
                    return self._batch_result(winning_batch, replay=True)
            return self._rejected("LABOR_CONFIRM_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def _validated_bank(
        self,
        org_id: uuid.UUID,
        account_code: str,
        transaction_id: uuid.UUID,
        payment_date: date,
        amount_fen: int,
    ) -> BankTransaction:
        self.finance._validate_bank_account(org_id, account_code, payment_date)
        bank = self.session.scalar(
            select(BankTransaction)
            .where(
                BankTransaction.org_id == org_id,
                BankTransaction.id == transaction_id,
            )
            .with_for_update()
        )
        if bank is None:
            raise ValueError("BANK_TRANSACTION_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
        if bank.import_action_id is None:
            raise ValueError("BANK_TRANSACTION_REQUIRES_CONTROLLED_IMPORT_ACTION")
        if bank.bank_account_code != account_code:
            raise ValueError("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
        if bank.amount_fen != -amount_fen:
            raise ValueError("BANK_TRANSACTION_AMOUNT_MISMATCH")
        if bank.booking_date != payment_date:
            raise ValueError("BANK_TRANSACTION_PAYMENT_DATE_MISMATCH")
        active_match = self.session.scalar(
            select(BankTransactionMatch)
            .where(
                BankTransactionMatch.org_id == org_id,
                BankTransactionMatch.bank_transaction_id == bank.id,
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
            .with_for_update()
        )
        if active_match is not None or bank.matched_event_id is not None:
            raise ValueError("BANK_TRANSACTION_ALREADY_MATCHED")
        return bank

    def _salary_request(
        self, request: PreviewUnifiedPayoutRunRequest, net_amount_fen: int
    ) -> RecordEventRequest:
        return RecordEventRequest.model_validate(
            {
                "org_id": str(request.org_id),
                "idempotency_key": f"{request.idempotency_key}:salary-derivation",
                "event_type": "salary_payment",
                "business_dates": {
                    "business_date": request.business_date,
                    "payment_date": request.payment_date,
                    "posting_date": request.posting_date,
                },
                "amounts": {"amount_fen": net_amount_fen},
                "bank_account_code": request.bank_account_code,
                "allocations": [
                    item.model_dump(mode="json") for item in request.salary_allocations
                ],
                "salary_withholding_allocations": [
                    item.model_dump(mode="json") for item in request.salary_withholding_allocations
                ],
                "description": request.description,
            }
        )

    def _derive_payout(self, request: PreviewUnifiedPayoutRunRequest) -> dict[str, Any]:
        self._evidence(request.org_id, request.evidence_references)
        derived_items: list[dict[str, Any]] = []
        salary_gross = sum(item.amount_fen for item in request.salary_allocations)
        salary_derived: dict[str, Any] | None = None
        if request.salary_allocations:
            supplied_by_item = {
                item.open_item_id: item for item in request.salary_withholding_allocations
            }
            salary_net = sum(
                allocation.amount_fen
                - sum(
                    supplied_by_item[
                        allocation.open_item_id
                    ].employee_social_insurance_items.values()
                )
                - sum(
                    supplied_by_item[allocation.open_item_id].employee_housing_fund_items.values()
                )
                - supplied_by_item[allocation.open_item_id].individual_income_tax_fen
                for allocation in request.salary_allocations
            )
            salary_request = self._salary_request(request, salary_net)
            salary_derived = self.finance._salary_payment_facts(salary_request)
            allocation_by_item = {
                item.open_item_id: item.amount_fen for item in request.salary_allocations
            }
            for allocation in salary_derived["allocations"]:
                source_id = uuid.UUID(allocation["open_item_id"])
                gross = allocation_by_item[source_id]
                social = sum(allocation["employee_social_insurance_items"].values())
                housing = sum(allocation["employee_housing_fund_items"].values())
                tax = int(allocation["individual_income_tax_fen"])
                open_item = self.session.get(OpenItem, source_id)
                if open_item is None:
                    raise ValueError("SALARY_OPEN_ITEM_NOT_FOUND")
                derived_items.append(
                    {
                        "item_kind": "salary",
                        "source_open_item_id": str(source_id),
                        "payroll_line_id": allocation["payroll_line_id"],
                        "labor_line_id": None,
                        "counterparty_id": str(open_item.counterparty_id),
                        "gross_amount_fen": gross,
                        "employee_social_insurance_fen": social,
                        "employee_housing_fund_fen": housing,
                        "individual_income_tax_fen": tax,
                        "net_amount_fen": gross - social - housing - tax,
                        "withholding_components": allocation,
                    }
                )
        for labor_item in request.labor_items:
            assert labor_item.source_open_item_id is not None
            source = self.session.scalar(
                select(OpenItem)
                .where(
                    OpenItem.org_id == request.org_id,
                    OpenItem.id == labor_item.source_open_item_id,
                )
                .with_for_update()
            )
            if source is None or source.payable_category != "labor_remuneration":
                raise ValueError("LABOR_PAYOUT_SOURCE_OPEN_ITEM_NOT_FOUND")
            available = source.original_amount_fen - source.settled_amount_fen
            if source.status not in {"open", "partial"} or available != source.original_amount_fen:
                raise ValueError("LABOR_PAYOUT_ONLY_SUPPORTS_FULL_UNPAID_SETTLEMENT")
            accrual_link = self.session.scalar(
                select(LaborRemunerationEventLink).where(
                    LaborRemunerationEventLink.org_id == request.org_id,
                    LaborRemunerationEventLink.event_id == source.source_event_id,
                    LaborRemunerationEventLink.link_kind == "accrual",
                )
            )
            if accrual_link is None:
                raise ValueError("LABOR_PAYOUT_SOURCE_LACKS_CONTROLLED_ACCRUAL")
            line = self.session.scalar(
                select(LaborRemunerationLine).where(
                    LaborRemunerationLine.org_id == request.org_id,
                    LaborRemunerationLine.batch_id == accrual_link.batch_id,
                    LaborRemunerationLine.counterparty_id == source.counterparty_id,
                )
            )
            if line is None or line.gross_remuneration_fen != source.original_amount_fen:
                raise ValueError("LABOR_PAYOUT_SOURCE_LINE_MISMATCH")
            entitlement = self.session.scalar(
                select(LaborWithholdingEntitlement).where(
                    LaborWithholdingEntitlement.org_id == request.org_id,
                    LaborWithholdingEntitlement.labor_line_id == line.id,
                )
            )
            if entitlement is None or entitlement.amount_fen != line.withholding_tax_fen:
                raise ValueError("LABOR_WITHHOLDING_ENTITLEMENT_MISMATCH")
            derived_items.append(
                {
                    "item_kind": "labor",
                    "source_open_item_id": str(source.id),
                    "payroll_line_id": None,
                    "labor_line_id": str(line.id),
                    "counterparty_id": str(source.counterparty_id),
                    "gross_amount_fen": source.original_amount_fen,
                    "employee_social_insurance_fen": 0,
                    "employee_housing_fund_fen": 0,
                    "individual_income_tax_fen": entitlement.amount_fen,
                    "net_amount_fen": source.original_amount_fen - entitlement.amount_fen,
                    "withholding_components": {
                        "entitlement_id": str(entitlement.id),
                        "batch_id": str(line.batch_id),
                    },
                }
            )
        if len({item["source_open_item_id"] for item in derived_items}) != len(derived_items):
            raise ValueError("DUPLICATE_PAYOUT_SOURCE_OPEN_ITEM")
        gross_total = sum(item["gross_amount_fen"] for item in derived_items)
        net_total = sum(item["net_amount_fen"] for item in derived_items)
        withholding_total = gross_total - net_total
        if salary_derived is not None and salary_derived["gross_salary_fen"] != salary_gross:
            raise ValueError("SALARY_DERIVATION_GROSS_MISMATCH")
        return {
            "items": derived_items,
            "salary_derived": salary_derived,
            "gross_total_fen": gross_total,
            "withholding_total_fen": withholding_total,
            "net_total_fen": net_total,
        }

    def preview_payout(self, request: PreviewUnifiedPayoutRunRequest) -> LaborResult:
        missing = request.missing_fields()
        if missing:
            return self._requirement(missing)
        payload_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                self._organization(request.org_id)
                assert request.posting_date is not None
                assert request.payment_date is not None
                assert request.bank_account_code is not None
                assert request.bank_transaction_id is not None
                assert_period_open(self.session, request.org_id, request.posting_date)
                existing = self.session.scalar(
                    select(UnifiedPayoutRun).where(
                        UnifiedPayoutRun.org_id == request.org_id,
                        UnifiedPayoutRun.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != payload_hash:
                        return self._rejected("PAYOUT_RUN_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    return self._payout_result(existing, replay=True)
                derived = self._derive_payout(request)
                self._validated_bank(
                    request.org_id,
                    request.bank_account_code,
                    request.bank_transaction_id,
                    request.payment_date,
                    derived["net_total_fen"],
                )
                calculation_input = {
                    "request": request.model_dump(mode="json"),
                    "derived": derived,
                }
                calculation_hash = self._hash(calculation_input)
                run = UnifiedPayoutRun(
                    org_id=request.org_id,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=payload_hash,
                    status="calculated",
                    calculation_hash=calculation_hash,
                    calculation_input=calculation_input,
                    calculation_trace=[
                        {
                            "stage": "unified_payout_exact_reconciliation",
                            "salary_item_count": sum(
                                item["item_kind"] == "salary" for item in derived["items"]
                            ),
                            "labor_item_count": sum(
                                item["item_kind"] == "labor" for item in derived["items"]
                            ),
                            "gross_total_fen": derived["gross_total_fen"],
                            "withholding_total_fen": derived["withholding_total_fen"],
                            "net_total_fen": derived["net_total_fen"],
                        }
                    ],
                    bank_account_code=request.bank_account_code,
                    bank_transaction_id=request.bank_transaction_id,
                    business_date=request.business_date,
                    payment_date=request.payment_date,
                    posting_date=request.posting_date,
                    gross_total_fen=derived["gross_total_fen"],
                    withholding_total_fen=derived["withholding_total_fen"],
                    net_total_fen=derived["net_total_fen"],
                )
                self.session.add(run)
                self.session.flush()
                for values in derived["items"]:
                    self.session.add(
                        UnifiedPayoutRunItem(
                            org_id=request.org_id,
                            payout_run_id=run.id,
                            item_kind=values["item_kind"],
                            source_open_item_id=uuid.UUID(values["source_open_item_id"]),
                            payroll_line_id=(
                                uuid.UUID(values["payroll_line_id"])
                                if values["payroll_line_id"]
                                else None
                            ),
                            labor_line_id=(
                                uuid.UUID(values["labor_line_id"])
                                if values["labor_line_id"]
                                else None
                            ),
                            counterparty_id=uuid.UUID(values["counterparty_id"]),
                            gross_amount_fen=values["gross_amount_fen"],
                            employee_social_insurance_fen=values["employee_social_insurance_fen"],
                            employee_housing_fund_fen=values["employee_housing_fund_fen"],
                            individual_income_tax_fen=values["individual_income_tax_fen"],
                            net_amount_fen=values["net_amount_fen"],
                            withholding_components=values["withholding_components"],
                        )
                    )
                for evidence_id in request.evidence_references:
                    self.session.add(
                        UnifiedPayoutRunEvidence(
                            org_id=request.org_id,
                            payout_run_id=run.id,
                            evidence_id=evidence_id,
                        )
                    )
                self.session.flush()
                return self._payout_result(run)
        except AccountingPeriodError as exc:
            return self._rejected(exc.code)
        except (IntegrityError, OperationalError):
            return self._rejected("PAYOUT_RUN_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def _payout_result(self, run: UnifiedPayoutRun, *, replay: bool = False) -> LaborResult:
        voucher = self.session.scalar(
            select(Voucher).where(Voucher.event_id == run.business_event_id)
        )
        items = self.session.scalars(
            select(UnifiedPayoutRunItem)
            .where(
                UnifiedPayoutRunItem.org_id == run.org_id,
                UnifiedPayoutRunItem.payout_run_id == run.id,
            )
            .order_by(UnifiedPayoutRunItem.id)
        ).all()
        return LaborResult(
            status=LaborResultStatus(run.status),
            payout_run_id=run.id,
            event_id=run.business_event_id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            calculation_hash=run.calculation_hash,
            trace=run.calculation_trace,
            data={
                "idempotent_replay": replay,
                "bank_transaction_id": str(run.bank_transaction_id),
                "gross_total_fen": run.gross_total_fen,
                "withholding_total_fen": run.withholding_total_fen,
                "net_total_fen": run.net_total_fen,
                "items": [
                    {
                        "id": str(item.id),
                        "item_kind": item.item_kind,
                        "source_open_item_id": str(item.source_open_item_id),
                        "payroll_line_id": (
                            str(item.payroll_line_id) if item.payroll_line_id else None
                        ),
                        "labor_line_id": (str(item.labor_line_id) if item.labor_line_id else None),
                        "gross_amount_fen": item.gross_amount_fen,
                        "employee_social_insurance_fen": (item.employee_social_insurance_fen),
                        "employee_housing_fund_fen": item.employee_housing_fund_fen,
                        "individual_income_tax_fen": item.individual_income_tax_fen,
                        "net_amount_fen": item.net_amount_fen,
                    }
                    for item in items
                ],
            },
        )

    @staticmethod
    def _following_month_day_15(payment_date: date) -> date:
        if payment_date.month == 12:
            return date(payment_date.year + 1, 1, 15)
        return date(payment_date.year, payment_date.month + 1, 15)

    def _agency(self, org_id: uuid.UUID, code: str, name: str) -> Counterparty:
        agency = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == "other",
                Counterparty.external_ref == code,
            )
        )
        display_name = f"法定缴费机构 {name}"
        if agency is not None:
            if agency.name != display_name:
                raise ValueError("LABOR_WITHHOLDING_AGENCY_IDENTITY_CONFLICT")
            return agency
        agency = Counterparty(
            org_id=org_id,
            kind="other",
            name=display_name,
            external_ref=code,
        )
        self.session.add(agency)
        self.session.flush()
        return agency

    def _settle(self, event: BusinessEvent, item: OpenItem, amount_fen: int) -> None:
        available = item.original_amount_fen - item.settled_amount_fen
        if item.status not in {"open", "partial"} or amount_fen > available:
            raise ValueError("PAYOUT_SOURCE_IS_NOT_AN_ACTIVE_OPEN_ITEM")
        item.settled_amount_fen += amount_fen
        item.status = (
            "settled" if item.settled_amount_fen == item.original_amount_fen else "partial"
        )
        self.session.add(
            Settlement(
                org_id=event.org_id,
                open_item_id=item.id,
                payment_event_id=event.id,
                amount_fen=amount_fen,
            )
        )

    def confirm_payout(self, request: ConfirmUnifiedPayoutRunRequest) -> LaborResult:
        request_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                existing_event = self.session.scalar(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.idempotency_key == request.idempotency_key,
                    )
                )
                if existing_event is not None:
                    if existing_event.request_payload_hash != request_hash:
                        return self._rejected("PAYOUT_CONFIRM_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    run = self.session.scalar(
                        select(UnifiedPayoutRun).where(
                            UnifiedPayoutRun.business_event_id == existing_event.id
                        )
                    )
                    if run is None:
                        return self._rejected("PAYOUT_CONFIRM_IDEMPOTENCY_SCOPE_CONFLICT")
                    return self._payout_result(run, replay=True)
                run = self.session.scalar(
                    select(UnifiedPayoutRun)
                    .where(
                        UnifiedPayoutRun.org_id == request.org_id,
                        UnifiedPayoutRun.id == request.payout_run_id,
                    )
                    .with_for_update()
                )
                if run is None:
                    return self._rejected("PAYOUT_RUN_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
                if run.status != "calculated" or run.business_event_id is not None:
                    winning_event = (
                        self.session.get(BusinessEvent, run.business_event_id)
                        if run.business_event_id is not None
                        else None
                    )
                    if (
                        winning_event is not None
                        and winning_event.idempotency_key == request.idempotency_key
                        and winning_event.request_payload_hash == request_hash
                    ):
                        return self._payout_result(run, replay=True)
                    return self._rejected("PAYOUT_RUN_IS_NOT_CONFIRMABLE")
                if request.calculation_hash != run.calculation_hash:
                    return self._rejected("PAYOUT_CALCULATION_HASH_MISMATCH")
                preview_request = PreviewUnifiedPayoutRunRequest.model_validate(
                    run.calculation_input["request"]
                )
                derived = self._derive_payout(preview_request)
                current_input = {
                    "request": preview_request.model_dump(mode="json"),
                    "derived": derived,
                }
                if self._hash(current_input) != run.calculation_hash:
                    return self._rejected("PAYOUT_SOURCE_STATE_OR_CALCULATION_CHANGED")
                assert_period_open(self.session, run.org_id, run.posting_date)
                bank = self._validated_bank(
                    run.org_id,
                    run.bank_account_code,
                    run.bank_transaction_id,
                    run.payment_date,
                    run.net_total_fen,
                )
                items = self.session.scalars(
                    select(UnifiedPayoutRunItem)
                    .where(
                        UnifiedPayoutRunItem.org_id == run.org_id,
                        UnifiedPayoutRunItem.payout_run_id == run.id,
                    )
                    .order_by(UnifiedPayoutRunItem.source_open_item_id)
                    .with_for_update()
                ).all()
                source_items = self.session.scalars(
                    select(OpenItem)
                    .where(
                        OpenItem.org_id == run.org_id,
                        OpenItem.id.in_([item.source_open_item_id for item in items]),
                    )
                    .order_by(OpenItem.id)
                    .with_for_update()
                ).all()
                source_by_id = {item.id: item for item in source_items}
                event = BusinessEvent(
                    org_id=run.org_id,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_hash,
                    event_type="unified_payout_run",
                    status="draft",
                    description=run.calculation_input["request"]["description"],
                    facts={
                        "payout_run_id": str(run.id),
                        "calculation_hash": run.calculation_hash,
                        "salary_item_count": sum(item.item_kind == "salary" for item in items),
                        "labor_item_count": sum(item.item_kind == "labor" for item in items),
                    },
                    business_date=run.business_date,
                    payment_date=run.payment_date,
                    posting_date=run.posting_date,
                    rule_trace=run.calculation_trace,
                    rule_version="unified-payout/1",
                )
                self.session.add(event)
                self.session.flush()
                evidence_ids = self.session.scalars(
                    select(UnifiedPayoutRunEvidence.evidence_id).where(
                        UnifiedPayoutRunEvidence.org_id == run.org_id,
                        UnifiedPayoutRunEvidence.payout_run_id == run.id,
                    )
                ).all()
                self.finance._attach_evidence(event, list(evidence_ids))
                entries: list[Entry] = []
                salary_gross = sum(
                    item.gross_amount_fen for item in items if item.item_kind == "salary"
                )
                if salary_gross:
                    entries.append(
                        Entry(account_role="employee_salary_payable", debit_fen=salary_gross)
                    )
                for item in items:
                    if item.item_kind == "labor":
                        entries.append(
                            Entry(
                                account_role="labor_remuneration_payable",
                                debit_fen=item.gross_amount_fen,
                                counterparty_id=item.counterparty_id,
                            )
                        )
                entries.append(
                    Entry(account_code=run.bank_account_code, credit_fen=run.net_total_fen)
                )
                for role, amount in (
                    (
                        "withheld_employee_social_payable",
                        sum(item.employee_social_insurance_fen for item in items),
                    ),
                    (
                        "withheld_employee_housing_fund_payable",
                        sum(item.employee_housing_fund_fen for item in items),
                    ),
                    (
                        "individual_income_tax_payable",
                        sum(item.individual_income_tax_fen for item in items),
                    ),
                ):
                    if amount:
                        entries.append(Entry(account_role=role, credit_fen=amount))
                for item in items:
                    source = source_by_id.get(item.source_open_item_id)
                    if source is None:
                        raise ValueError("PAYOUT_SOURCE_OPEN_ITEM_NOT_FOUND")
                    self._settle(event, source, item.gross_amount_fen)

                salary_derived = derived["salary_derived"]
                if salary_derived is not None:
                    self.finance._record_payroll_withholding_allocations(event, salary_derived)
                    batch_id = uuid.UUID(salary_derived["payroll_batch_id"])
                    for item in items:
                        if item.item_kind == "salary":
                            self.session.add(
                                PayrollEventLink(
                                    org_id=run.org_id,
                                    event_id=event.id,
                                    payroll_batch_id=batch_id,
                                    source_open_item_id=item.source_open_item_id,
                                    link_kind="salary_payment",
                                )
                            )
                    salary_for_plans = {
                        **salary_derived,
                        "salary_withholding_allocations": salary_derived["allocations"],
                    }
                    create_open_items(
                        self.session,
                        event=event,
                        plans=self.finance._salary_withholding_open_item_plans(
                            event, salary_for_plans
                        ),
                    )

                agency: Counterparty | None = None
                labor_tax_plans: list[OpenItemPlan] = []
                labor_tax_sources: list[tuple[UnifiedPayoutRunItem, LaborRemunerationLine]] = []
                if any(
                    item.item_kind == "labor" and item.individual_income_tax_fen for item in items
                ):
                    agency = self._agency(
                        run.org_id,
                        run.calculation_input["request"]["withholding_agency_code"],
                        run.calculation_input["request"]["withholding_agency_name"],
                    )
                for item in items:
                    if item.item_kind != "labor":
                        continue
                    assert item.labor_line_id is not None
                    line = self.session.get(LaborRemunerationLine, item.labor_line_id)
                    if line is None:
                        raise ValueError("LABOR_PAYOUT_LINE_NOT_FOUND")
                    self.session.add(
                        LaborRemunerationEventLink(
                            org_id=run.org_id,
                            event_id=event.id,
                            batch_id=line.batch_id,
                            labor_line_id=line.id,
                            source_open_item_id=item.source_open_item_id,
                            link_kind="payment",
                        )
                    )
                    if item.individual_income_tax_fen:
                        assert agency is not None
                        labor_tax_plans.append(
                            OpenItemPlan(
                                counterparty_id=agency.id,
                                item_type="payable",
                                original_amount_fen=item.individual_income_tax_fen,
                                due_date=self._following_month_day_15(run.payment_date),
                                payable_category="labor_individual_income_tax",
                                payable_agency_code=run.calculation_input["request"][
                                    "withholding_agency_code"
                                ],
                            )
                        )
                        labor_tax_sources.append((item, line))
                labor_tax_open_items = create_open_items(
                    self.session, event=event, plans=labor_tax_plans
                )
                for tax_open_item, (run_item, line) in zip(
                    labor_tax_open_items, labor_tax_sources, strict=True
                ):
                    entitlement = self.session.scalar(
                        select(LaborWithholdingEntitlement).where(
                            LaborWithholdingEntitlement.org_id == run.org_id,
                            LaborWithholdingEntitlement.labor_line_id == line.id,
                        )
                    )
                    if entitlement is None:
                        raise ValueError("LABOR_WITHHOLDING_ENTITLEMENT_NOT_FOUND")
                    self.session.add(
                        LaborWithholdingOpenItemSource(
                            org_id=run.org_id,
                            open_item_id=tax_open_item.id,
                            entitlement_id=entitlement.id,
                            labor_line_id=line.id,
                            payment_event_id=event.id,
                            amount_fen=run_item.individual_income_tax_fen,
                        )
                    )
                self.session.add(
                    BankTransactionMatch(
                        org_id=run.org_id,
                        bank_transaction_id=bank.id,
                        event_id=event.id,
                    )
                )
                bank.matched_event_id = event.id
                voucher = create_voucher(
                    self.session,
                    event=event,
                    posting_date=run.posting_date,
                    description=event.description,
                    entries=entries,
                )
                run.status = "posted"
                run.business_event_id = event.id
                run.confirmed_at = datetime.now(UTC)
                run.confirmation_note = request.confirmation_note
                event.status = "posted"
                self.session.add(
                    AuditLog(
                        org_id=run.org_id,
                        event_id=event.id,
                        action="unified_payout_confirmed",
                        details={
                            "payout_run_id": str(run.id),
                            "bank_transaction_id": str(bank.id),
                            "calculation_hash": run.calculation_hash,
                        },
                    )
                )
                self.session.flush()
                result = self._payout_result(run)
                result.voucher_id = voucher.id
                result.voucher_number = voucher.voucher_number
                return result
        except AccountingPeriodError as exc:
            return self._rejected(exc.code)
        except (IntegrityError, OperationalError):
            winning_event = self.session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == request.org_id,
                    BusinessEvent.idempotency_key == request.idempotency_key,
                )
            )
            if winning_event is not None and winning_event.request_payload_hash == request_hash:
                winning_run = self.session.scalar(
                    select(UnifiedPayoutRun).where(
                        UnifiedPayoutRun.business_event_id == winning_event.id
                    )
                )
                if winning_run is not None:
                    return self._payout_result(winning_run, replay=True)
            return self._rejected("PAYOUT_CONFIRM_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def pay_withholding_tax(self, request: PayLaborWithholdingTaxRequest) -> LaborResult:
        missing = request.missing_fields()
        if missing:
            return self._requirement(missing)
        request_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                self._organization(request.org_id)
                existing = self.session.scalar(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != request_hash:
                        return self._rejected("LABOR_TAX_PAYMENT_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    voucher = self.session.scalar(
                        select(Voucher).where(Voucher.event_id == existing.id)
                    )
                    return LaborResult(
                        status=LaborResultStatus.POSTED,
                        event_id=existing.id,
                        voucher_id=voucher.id if voucher else None,
                        voucher_number=voucher.voucher_number if voucher else None,
                        data={"idempotent_replay": True},
                    )
                assert request.posting_date is not None
                assert request.payment_date is not None
                assert request.business_date is not None
                assert request.amount_fen is not None
                assert request.bank_account_code is not None
                assert request.bank_transaction_id is not None
                assert_period_open(self.session, request.org_id, request.posting_date)
                self._evidence(request.org_id, request.evidence_references)
                if sum(item.amount_fen for item in request.allocations) != request.amount_fen:
                    return self._rejected("LABOR_TAX_PAYMENT_ALLOCATIONS_MUST_EQUAL_AMOUNT")
                bank = self._validated_bank(
                    request.org_id,
                    request.bank_account_code,
                    request.bank_transaction_id,
                    request.payment_date,
                    request.amount_fen,
                )
                open_items = self.session.scalars(
                    select(OpenItem)
                    .where(
                        OpenItem.org_id == request.org_id,
                        OpenItem.id.in_([item.open_item_id for item in request.allocations]),
                    )
                    .order_by(OpenItem.id)
                    .with_for_update()
                ).all()
                by_id = {item.id: item for item in open_items}
                event = BusinessEvent(
                    org_id=request.org_id,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_hash,
                    event_type="labor_withholding_tax_payment",
                    status="draft",
                    description=request.description,
                    facts=request.model_dump(mode="json"),
                    business_date=request.business_date,
                    payment_date=request.payment_date,
                    posting_date=request.posting_date,
                    rule_trace=[
                        {
                            "stage": "labor_withholding_tax_sources_validated",
                            "allocation_count": len(request.allocations),
                            "amount_fen": request.amount_fen,
                        }
                    ],
                    rule_version="labor-withholding-payment/1",
                )
                self.session.add(event)
                self.session.flush()
                self.finance._attach_evidence(event, request.evidence_references)
                for allocation in request.allocations:
                    item = by_id.get(allocation.open_item_id)
                    if (
                        item is None
                        or item.payable_category != "labor_individual_income_tax"
                        or item.status not in {"open", "partial"}
                    ):
                        raise ValueError("LABOR_TAX_PAYMENT_SOURCE_IS_NOT_ACTIVE_LABOR_TAX")
                    tax_source = self.session.scalar(
                        select(LaborWithholdingOpenItemSource).where(
                            LaborWithholdingOpenItemSource.org_id == request.org_id,
                            LaborWithholdingOpenItemSource.open_item_id == item.id,
                        )
                    )
                    if tax_source is None:
                        raise ValueError("LABOR_TAX_PAYMENT_SOURCE_LACKS_PAYMENT_LINEAGE")
                    entitlement = self.session.scalar(
                        select(LaborWithholdingEntitlement).where(
                            LaborWithholdingEntitlement.org_id == request.org_id,
                            LaborWithholdingEntitlement.id == tax_source.entitlement_id,
                            LaborWithholdingEntitlement.labor_line_id == tax_source.labor_line_id,
                        )
                    )
                    if entitlement is None:
                        raise ValueError("LABOR_TAX_PAYMENT_ENTITLEMENT_NOT_FOUND")
                    tax_line = self.session.get(LaborRemunerationLine, tax_source.labor_line_id)
                    if tax_line is None or tax_line.org_id != request.org_id:
                        raise ValueError("LABOR_TAX_PAYMENT_SOURCE_LINE_NOT_FOUND")
                    paid = self.session.scalar(
                        select(
                            func.coalesce(
                                func.sum(LaborWithholdingTaxPaymentAllocation.amount_fen), 0
                            )
                        ).where(
                            LaborWithholdingTaxPaymentAllocation.org_id == request.org_id,
                            LaborWithholdingTaxPaymentAllocation.entitlement_id == entitlement.id,
                            LaborWithholdingTaxPaymentAllocation.reversed.is_(False),
                        )
                    )
                    if int(paid or 0) + allocation.amount_fen > entitlement.amount_fen:
                        raise ValueError("LABOR_TAX_PAYMENT_EXCEEDS_WITHHOLDING_ENTITLEMENT")
                    self._settle(event, item, allocation.amount_fen)
                    self.session.add(
                        LaborWithholdingTaxPaymentAllocation(
                            org_id=request.org_id,
                            entitlement_id=entitlement.id,
                            open_item_id=item.id,
                            payment_event_id=event.id,
                            amount_fen=allocation.amount_fen,
                        )
                    )
                    self.session.add(
                        LaborRemunerationEventLink(
                            org_id=request.org_id,
                            event_id=event.id,
                            batch_id=tax_line.batch_id,
                            labor_line_id=tax_source.labor_line_id,
                            source_open_item_id=item.id,
                            source_payment_event_id=tax_source.payment_event_id,
                            link_kind="tax_payment",
                        )
                    )
                self.session.add(
                    BankTransactionMatch(
                        org_id=request.org_id,
                        bank_transaction_id=bank.id,
                        event_id=event.id,
                    )
                )
                bank.matched_event_id = event.id
                voucher = create_voucher(
                    self.session,
                    event=event,
                    posting_date=request.posting_date,
                    description=request.description,
                    entries=[
                        Entry(
                            account_role="individual_income_tax_payable",
                            debit_fen=request.amount_fen,
                        ),
                        Entry(
                            account_code=request.bank_account_code,
                            credit_fen=request.amount_fen,
                        ),
                    ],
                )
                event.status = "posted"
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        event_id=event.id,
                        action="labor_withholding_tax_paid",
                        details={"amount_fen": request.amount_fen},
                    )
                )
                self.session.flush()
                return LaborResult(
                    status=LaborResultStatus.POSTED,
                    event_id=event.id,
                    voucher_id=voucher.id,
                    voucher_number=voucher.voucher_number,
                    trace=event.rule_trace,
                )
        except AccountingPeriodError as exc:
            return self._rejected(exc.code)
        except (IntegrityError, OperationalError):
            return self._rejected("LABOR_TAX_PAYMENT_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def confirm_external_declaration(
        self, request: ConfirmLaborExternalDeclarationRequest
    ) -> LaborResult:
        """Append evidence of external filing without rewriting the accrual snapshot."""

        request_hash = self._hash(request.model_dump(mode="json"))
        try:
            with self.session.begin_nested():
                self._organization(request.org_id)
                existing = self.session.scalar(
                    select(LaborExternalDeclarationConfirmation).where(
                        LaborExternalDeclarationConfirmation.org_id == request.org_id,
                        LaborExternalDeclarationConfirmation.idempotency_key
                        == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != request_hash:
                        return self._rejected("LABOR_DECLARATION_IDEMPOTENCY_PAYLOAD_MISMATCH")
                    return LaborResult(
                        status=LaborResultStatus.POSTED,
                        data={
                            "confirmation_id": str(existing.id),
                            "labor_line_id": str(existing.labor_line_id),
                            "idempotent_replay": True,
                        },
                    )
                self._evidence(request.org_id, request.evidence_references)
                line = self.session.scalar(
                    select(LaborRemunerationLine).where(
                        LaborRemunerationLine.org_id == request.org_id,
                        LaborRemunerationLine.id == request.labor_line_id,
                    )
                )
                if line is None:
                    return self._rejected(
                        "LABOR_DECLARATION_LINE_NOT_FOUND_OR_ORGANIZATION_MISMATCH"
                    )
                batch = self.session.get(LaborRemunerationBatch, line.batch_id)
                if batch is None or batch.status != "posted":
                    return self._rejected("LABOR_DECLARATION_REQUIRES_POSTED_BATCH")
                payout = self.session.scalar(
                    select(UnifiedPayoutRun)
                    .join(
                        UnifiedPayoutRunItem,
                        (UnifiedPayoutRunItem.org_id == UnifiedPayoutRun.org_id)
                        & (UnifiedPayoutRunItem.payout_run_id == UnifiedPayoutRun.id),
                    )
                    .where(
                        UnifiedPayoutRun.org_id == request.org_id,
                        UnifiedPayoutRunItem.labor_line_id == line.id,
                        UnifiedPayoutRun.status == "posted",
                    )
                )
                if payout is None:
                    return self._rejected("LABOR_DECLARATION_REQUIRES_POSTED_PAYOUT")
                if request.declaration_date < payout.payment_date:
                    return self._rejected("LABOR_DECLARATION_PRECEDES_PAYMENT")
                already_confirmed = self.session.scalar(
                    select(LaborExternalDeclarationConfirmation.id).where(
                        LaborExternalDeclarationConfirmation.org_id == request.org_id,
                        LaborExternalDeclarationConfirmation.labor_line_id == line.id,
                    )
                )
                if already_confirmed is not None:
                    return self._rejected("LABOR_DECLARATION_ALREADY_CONFIRMED")
                confirmation = LaborExternalDeclarationConfirmation(
                    org_id=request.org_id,
                    labor_line_id=line.id,
                    declaration_date=request.declaration_date,
                    external_declaration_reference=(request.external_declaration_reference),
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=request_hash,
                )
                self.session.add(confirmation)
                self.session.flush()
                for evidence_id in request.evidence_references:
                    self.session.add(
                        LaborExternalDeclarationEvidence(
                            org_id=request.org_id,
                            confirmation_id=confirmation.id,
                            evidence_id=evidence_id,
                        )
                    )
                self.session.flush()
                return LaborResult(
                    status=LaborResultStatus.POSTED,
                    data={
                        "confirmation_id": str(confirmation.id),
                        "labor_line_id": str(line.id),
                        "external_declaration_status": "confirmed",
                        "external_declaration_reference": (
                            confirmation.external_declaration_reference
                        ),
                    },
                )
        except (IntegrityError, OperationalError):
            return self._rejected("LABOR_DECLARATION_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def get(self, request: GetLaborRemunerationRequest) -> LaborResult:
        if request.labor_person_id is not None:
            person = self.session.scalar(
                select(LaborServicePerson).where(
                    LaborServicePerson.org_id == request.org_id,
                    LaborServicePerson.id == request.labor_person_id,
                )
            )
            if person is None:
                return self._rejected("LABOR_PERSON_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
            return self._result_for_person(person)
        if request.batch_id is not None:
            batch = self.session.scalar(
                select(LaborRemunerationBatch).where(
                    LaborRemunerationBatch.org_id == request.org_id,
                    LaborRemunerationBatch.id == request.batch_id,
                )
            )
            if batch is None:
                return self._rejected("LABOR_BATCH_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
            return self._batch_result(batch)
        run = self.session.scalar(
            select(UnifiedPayoutRun).where(
                UnifiedPayoutRun.org_id == request.org_id,
                UnifiedPayoutRun.id == request.payout_run_id,
            )
        )
        if run is None:
            return self._rejected("PAYOUT_RUN_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
        return self._payout_result(run)

    def reverse_event(self, request: ReverseEventRequest):
        """Use the common reversal engine, then close module-specific audit state."""

        original = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.id == request.event_id,
            )
        )
        if original is None:
            return self.finance.reverse_event(request)
        original_type = original.event_type
        result = self.finance._reverse_event_write(request)
        if result.status is not ResultStatus.POSTED or result.event_id is None:
            return result
        reversal_id = result.event_id
        if original_type == "labor_remuneration_accrual":
            batch = self.session.scalar(
                select(LaborRemunerationBatch).where(
                    LaborRemunerationBatch.org_id == request.org_id,
                    LaborRemunerationBatch.business_event_id == original.id,
                )
            )
            if batch is not None:
                batch.status = "reversed"
        elif original_type == "unified_payout_run":
            run = self.session.scalar(
                select(UnifiedPayoutRun).where(
                    UnifiedPayoutRun.org_id == request.org_id,
                    UnifiedPayoutRun.business_event_id == original.id,
                )
            )
            if run is not None:
                run.status = "reversed"
            allocations = self.session.scalars(
                select(PayrollWithholdingPaymentAllocation).where(
                    PayrollWithholdingPaymentAllocation.org_id == request.org_id,
                    PayrollWithholdingPaymentAllocation.payment_event_id == original.id,
                    PayrollWithholdingPaymentAllocation.reversed.is_(False),
                )
            ).all()
            for allocation in allocations:
                allocation.reversed = True
                allocation.reversed_by_event_id = reversal_id
        elif original_type == "labor_withholding_tax_payment":
            allocations = self.session.scalars(
                select(LaborWithholdingTaxPaymentAllocation).where(
                    LaborWithholdingTaxPaymentAllocation.org_id == request.org_id,
                    LaborWithholdingTaxPaymentAllocation.payment_event_id == original.id,
                    LaborWithholdingTaxPaymentAllocation.reversed.is_(False),
                )
            ).all()
            for allocation in allocations:
                allocation.reversed = True
                allocation.reversed_by_event_id = reversal_id
        original_links = self.session.scalars(
            select(LaborRemunerationEventLink).where(
                LaborRemunerationEventLink.org_id == request.org_id,
                LaborRemunerationEventLink.event_id == original.id,
                LaborRemunerationEventLink.link_kind != "reversal",
            )
        ).all()
        for link in original_links:
            exists = self.session.scalar(
                select(LaborRemunerationEventLink.id).where(
                    LaborRemunerationEventLink.org_id == request.org_id,
                    LaborRemunerationEventLink.event_id == reversal_id,
                    LaborRemunerationEventLink.batch_id == link.batch_id,
                    LaborRemunerationEventLink.labor_line_id == link.labor_line_id,
                    LaborRemunerationEventLink.link_kind == "reversal",
                )
            )
            if exists is None:
                self.session.add(
                    LaborRemunerationEventLink(
                        org_id=request.org_id,
                        event_id=reversal_id,
                        batch_id=link.batch_id,
                        labor_line_id=link.labor_line_id,
                        source_payment_event_id=original.id,
                        link_kind="reversal",
                    )
                )
        self.session.flush()
        return result
