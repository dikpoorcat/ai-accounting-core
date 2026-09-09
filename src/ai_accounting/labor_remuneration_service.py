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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .component_schemas import RecordEventRequest
from .labor_remuneration_schemas import (
    ConfirmLaborExternalDeclarationRequest,
    ConfirmLaborRemunerationBatchRequest,
    EndLaborServicePersonRequest,
    GetLaborRemunerationRequest,
    LaborInformationRequirement,
    LaborResult,
    LaborResultStatus,
    PreviewLaborRemunerationBatchRequest,
    RegisterLaborServicePersonRequest,
)
from .ledger import (
    AccountingPeriodError,
    ComponentPostingPlan,
    Entry,
    OpenItemPlan,
    assert_period_open,
)
from .models import (
    AuditLog,
    BusinessEvent,
    BusinessEventComponent,
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
    Organization,
    Voucher,
)
from .schemas import ResultStatus, ReverseEventRequest
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
    def _accounting_batch_request(request: PreviewLaborRemunerationBatchRequest) -> dict[str, Any]:
        return request.model_dump(
            mode="json",
            exclude={
                "description": True,
                "planned_payment_date": True,
                "items": {
                    "__all__": {
                        "external_declaration_status",
                        "external_declaration_reference",
                    }
                },
            },
        )

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
        payload_hash = self._hash(request.model_dump(mode="json", exclude={"person_code"}))
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
                if request.person_code is not None:
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
        assert request.business_date is not None
        policy = self._active_policy(request.business_date)
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
            assert item.service_start_date is not None and item.service_end_date is not None
            if item.service_start_date < person.relationship_start_date:
                raise ValueError("LABOR_SERVICE_OUTSIDE_RELATIONSHIP_PERIOD")
            if (
                person.relationship_end_date is not None
                and item.service_end_date > person.relationship_end_date
            ):
                raise ValueError("LABOR_SERVICE_OUTSIDE_RELATIONSHIP_PERIOD")
            linked_employees = list(
                self.session.scalars(
                    select(Employee).where(
                        Employee.org_id == request.org_id,
                        (
                            (Employee.prior_labor_person_id == person.id)
                            | (Employee.counterparty_id == person.counterparty_id)
                        ),
                    )
                )
            )
            if any(
                item.service_end_date >= employee.employment_start_date
                and (
                    employee.employment_end_date is None
                    or item.service_start_date <= employee.employment_end_date
                )
                for employee in linked_employees
            ):
                raise ValueError("LABOR_SERVICE_OVERLAPS_EMPLOYEE_PAYROLL_PERIOD")
            if item.tax_identity == "nonresident":
                raise ValueError("NONRESIDENT_LABOR_REMUNERATION_NOT_SUPPORTED")
            if item.is_full_time_student is True:
                raise ValueError("STUDENT_INTERNSHIP_WITHHOLDING_METHOD_NOT_SUPPORTED")
            if item.tax_identity != "resident" or item.is_full_time_student is not False:
                raise ValueError("LABOR_TAX_IDENTITY_IS_NOT_SUPPORTED")
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
        accounting_request = self._accounting_batch_request(request)
        calculation_input = {
            "request": accounting_request,
            "policy": policy_snapshot,
            "lines": derived_lines,
        }
        return policy, calculation_input, derived_lines

    def preview_batch(self, request: PreviewLaborRemunerationBatchRequest) -> LaborResult:
        missing = request.missing_fields()
        if missing:
            return self._requirement(missing)
        payload_hash = self._hash(self._accounting_batch_request(request))
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
                "planned_payment_date": (
                    batch.planned_payment_date.isoformat()
                    if batch.planned_payment_date
                    else None
                ),
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

    def compile_accrual_component(self, component) -> tuple[ComponentPostingPlan, list[uuid.UUID]]:
        from .component_service import MissingFacts

        request = self._active_component_request
        batch = self.session.scalar(
            select(LaborRemunerationBatch)
            .where(
                LaborRemunerationBatch.org_id == request.org_id,
                LaborRemunerationBatch.id == component.batch_id,
            )
            .with_for_update()
        )
        if batch is None:
            raise ValueError("LABOR_BATCH_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
        if batch.status != "calculated" or batch.business_event_id is not None:
            raise ValueError("LABOR_BATCH_IS_NOT_CONFIRMABLE")
        if component.calculation_hash != batch.calculation_hash:
            raise ValueError("LABOR_CALCULATION_HASH_MISMATCH")
        if self._hash(batch.calculation_input) != batch.calculation_hash:
            raise ValueError("LABOR_CALCULATION_SNAPSHOT_TAMPERED")
        if request.posting_date != batch.posting_date:
            raise ValueError("LABOR_COMPONENT_POSTING_DATE_MISMATCH")
        if component.business_date != batch.business_date:
            raise ValueError("LABOR_COMPONENT_BUSINESS_DATE_MISMATCH")
        lines = list(
            self.session.scalars(
                select(LaborRemunerationLine)
                .where(
                    LaborRemunerationLine.org_id == batch.org_id,
                    LaborRemunerationLine.batch_id == batch.id,
                )
                .order_by(LaborRemunerationLine.id)
                .with_for_update()
            )
        )
        if not lines:
            raise ValueError("LABOR_CALCULATION_SNAPSHOT_TAMPERED")
        evidence_ids = list(
            self.session.scalars(
                select(LaborRemunerationBatchEvidence.evidence_id)
                .where(
                    LaborRemunerationBatchEvidence.org_id == batch.org_id,
                    LaborRemunerationBatchEvidence.batch_id == batch.id,
                )
                .order_by(LaborRemunerationBatchEvidence.evidence_id)
            )
        )
        requested_ids = sorted(
            uuid.UUID(value)
            for value in batch.calculation_input["request"].get("evidence_references", [])
        )
        if sorted(evidence_ids) != requested_ids:
            raise ValueError("LABOR_CALCULATION_SNAPSHOT_TAMPERED")
        if not evidence_ids:
            raise MissingFacts([f"components.{component.key}.evidence_references"])
        entries: list[Entry] = []
        open_items: list[OpenItemPlan] = []
        entitlements: list[LaborWithholdingEntitlement] = []
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
            open_items.append(
                OpenItemPlan(
                    counterparty_id=line.counterparty_id,
                    item_type="payable",
                    original_amount_fen=line.gross_remuneration_fen,
                    due_date=None,
                    payable_category="labor_remuneration",
                    key=str(line.id),
                    account_role="labor_remuneration_payable",
                )
            )
            entitlement = LaborWithholdingEntitlement(
                id=uuid.uuid4(),
                org_id=batch.org_id,
                labor_line_id=line.id,
                amount_fen=line.withholding_tax_fen,
            )
            self.session.add(entitlement)
            entitlements.append(entitlement)
        derived = {
            "batch_id": str(batch.id),
            "calculation_hash": batch.calculation_hash,
            "labor_sources": [
                {
                    "open_item_key": str(line.id),
                    "labor_line_id": str(line.id),
                    "counterparty_id": str(line.counterparty_id),
                    "gross_remuneration_fen": line.gross_remuneration_fen,
                    "withholding_entitlement_id": str(entitlement.id),
                    "withholding_tax_fen": entitlement.amount_fen,
                }
                for line, entitlement in zip(lines, entitlements, strict=True)
            ],
        }

        def apply(session, event, persisted):
            session.add(
                LaborRemunerationEventLink(
                    org_id=batch.org_id,
                    event_id=event.id,
                    component_id=persisted.id,
                    batch_id=batch.id,
                    link_kind="accrual",
                )
            )
            batch.status = "posted"
            batch.business_event_id = event.id
            batch.confirmation_note = None
            batch.confirmed_at = datetime.now(UTC)
            session.add(
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

        return (
            ComponentPostingPlan(
                key=component.key,
                kind=component.kind,
                facts=component.model_dump(mode="json"),
                derived=derived,
                rule_version=f"{RULE_VERSION_PREFIX}{batch.policy_snapshot['version']}",
                entries=entries,
                open_items=open_items,
                effects=[apply],
            ),
            evidence_ids,
        )

    def confirm_batch(self, request: ConfirmLaborRemunerationBatchRequest) -> LaborResult:
        from .component_service import ComponentService

        batch = self.session.scalar(
            select(LaborRemunerationBatch).where(
                LaborRemunerationBatch.org_id == request.org_id,
                LaborRemunerationBatch.id == request.batch_id,
            )
        )
        if batch is None:
            return self._rejected("LABOR_BATCH_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
        evidence_ids = list(
            self.session.scalars(
                select(LaborRemunerationBatchEvidence.evidence_id)
                .where(
                    LaborRemunerationBatchEvidence.org_id == batch.org_id,
                    LaborRemunerationBatchEvidence.batch_id == batch.id,
                )
                .order_by(LaborRemunerationBatchEvidence.evidence_id)
            )
        )
        component_request = RecordEventRequest.model_validate(
            {
                "org_id": request.org_id,
                "idempotency_key": request.idempotency_key,
                "posting_date": batch.posting_date,
                "description": "",
                "components": [
                    {
                        "key": "labor_remuneration_accrual",
                        "kind": "labor_remuneration_accrual",
                        "business_date": batch.business_date,
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
                if result.status == ResultStatus.NEEDS_INFORMATION:
                    return LaborResult(
                        status=LaborResultStatus.NEEDS_INFORMATION,
                        missing_information=result.missing_information,
                    )
                if result.status != ResultStatus.POSTED:
                    errors = [
                        "LABOR_CONFIRM_IDEMPOTENCY_PAYLOAD_MISMATCH"
                        if code == "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
                        else code
                        for code in result.errors
                    ]
                    return self._rejected(*errors)
                self.session.refresh(batch)
                return self._batch_result(
                    batch, replay=bool(result.data.get("idempotent_replay"))
                )
        except AccountingPeriodError as exc:
            return self._rejected(exc.code)
        except (IntegrityError, OperationalError):
            return self._rejected("LABOR_CONFIRM_CONCURRENT_WRITE_CONFLICT")
        except ValueError as exc:
            return self._rejected(str(exc))

    def confirm_external_declaration(
        self, request: ConfirmLaborExternalDeclarationRequest
    ) -> LaborResult:
        """Append evidence of external filing without rewriting the accrual snapshot."""

        request_hash = self._hash(
            request.model_dump(mode="json", exclude={"external_declaration_reference"})
        )
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
                payment_components = self.session.scalars(
                    select(BusinessEventComponent)
                    .join(
                        LaborRemunerationEventLink,
                        LaborRemunerationEventLink.component_id == BusinessEventComponent.id,
                    )
                    .join(BusinessEvent, BusinessEvent.id == BusinessEventComponent.event_id)
                    .where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.status == "posted",
                        LaborRemunerationEventLink.org_id == request.org_id,
                        LaborRemunerationEventLink.labor_line_id == line.id,
                        LaborRemunerationEventLink.link_kind == "payment",
                    )
                ).all()
                if not payment_components:
                    return self._rejected("LABOR_DECLARATION_REQUIRES_POSTED_PAYMENT")
                payment_date = max(
                    date.fromisoformat(component.derived["payment_date"])
                    for component in payment_components
                )
                if request.declaration_date < payment_date:
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
        raise ValueError("labor identity is required")

    def reverse_event(self, request: ReverseEventRequest):
        return FinanceService.reverse_event(self.finance, request)
