"""Typed tax confirmations compiled for the shared component committer."""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from .enterprise_income_tax import RULE, EnterpriseIncomeTaxService, digest, lock_income_tax
from .enterprise_income_tax_schemas import (
    ConfirmEnterpriseIncomeTaxResultRequest,
    PreviewEnterpriseIncomeTaxResultRequest,
)
from .financial_statement_schemas import (
    ConfirmEnterpriseIncomeTaxQuarterRequest,
    EnterpriseIncomeTaxTreatment,
)
from .financial_statements import (
    ACCOUNTING_RULE_SOURCE_URL,
    ACCOUNTING_RULE_VERSION,
    _hash,
)
from .ledger import ComponentPostingPlan, Entry, account_balance_fen
from .models import (
    AuditLog,
    BusinessEvent,
    BusinessEventComponent,
    EnterpriseIncomeTaxQuarterConfirmation,
    EnterpriseIncomeTaxResult,
    Organization,
    TaxPeriod,
    TaxPeriodSource,
)
from .organization_profiles import profile_as_of
from .schemas import TaxPeriodConfirmRequest
from .tax import (
    calculate_tax_period,
    calculate_tax_period_from_sources,
    tax_period_sources,
)
from .tax_accounts import vat_relief_entries


def _component_payload(compiler, component) -> dict:
    return component.model_dump(mode="json")


def _request_evidence(compiler, component) -> list[uuid.UUID]:
    return sorted(
        set(compiler.request.evidence_references) | set(component.evidence_references),
        key=str,
    )


def compile_tax_relief(compiler, component) -> ComponentPostingPlan:
    """Recalculate and persist one exact VAT/surtax period snapshot."""

    session = compiler.session
    request = compiler.request
    if component.business_date != component.end_date:
        raise ValueError("TAX_PERIOD_BUSINESS_DATE_MISMATCH")
    if request.posting_date < component.end_date:
        raise ValueError("TAX_PERIOD_POSTING_DATE_PRECEDES_END")
    compiler.common._lock_tax_period_org(request.org_id)
    organization = session.scalar(
        select(Organization)
        .where(Organization.id == request.org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if organization is None:
        raise ValueError("ORGANIZATION_NOT_FOUND")
    posted_result = calculate_tax_period(
        session,
        organization,
        component.start_date,
        component.end_date,
        request.posting_date,
    )
    posted_calculation = posted_result.to_dict()
    actual_sources = list(posted_calculation["source_event_snapshots"])
    review_sources = list(posted_calculation["source_review_snapshots"])
    for source in compiler.plans.values():
        gross_fen = int(source.derived.get("taxable_gross_fen", 0))
        obligation_value = source.derived.get("tax_obligation_date")
        if not gross_fen or obligation_value is None:
            continue
        obligation_date = date.fromisoformat(str(obligation_value))
        if not component.start_date <= obligation_date <= component.end_date:
            continue
        review_sources.append(
            {
                "event_idempotency_key": request.idempotency_key,
                "component_key": source.key,
                "gross_fen": gross_fen,
                "net_fen": int(source.derived["net_sales_fen"]),
                "vat_fen": int(source.derived["vat_fen"]),
                "exemption_eligible": bool(source.derived.get("exemption_eligible", False)),
            }
        )
    review_sources.sort(key=lambda row: (row["event_idempotency_key"], row["component_key"]))
    tax_result = calculate_tax_period_from_sources(
        session,
        organization,
        component.start_date,
        component.end_date,
        request.posting_date,
        source_event_snapshots=actual_sources,
        source_review_snapshots=review_sources,
    )
    if not compiler.previewing and component.calculation_hash != tax_result.calculation_hash:
        raise ValueError("TAX_PERIOD_CALCULATION_STALE")
    conflict_request = TaxPeriodConfirmRequest(
        org_id=request.org_id,
        start_date=component.start_date,
        end_date=component.end_date,
        adjustment_posting_date=request.posting_date,
        calculation_hash=tax_result.calculation_hash,
        idempotency_key=request.idempotency_key,
    )
    if conflict := compiler.common._active_tax_period_conflict(conflict_request, lock=True):
        raise ValueError(conflict)
    entries: list[Entry] = []
    if tax_result.vat_relief_fen:
        entries.extend(
            vat_relief_entries(
                session,
                request.org_id,
                tax_result,
                pending_plans=compiler.plans.values(),
                pending_event_idempotency_key=request.idempotency_key,
            )
        )
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
                Entry(
                    account_role="surtax_payable",
                    credit_fen=tax_result.surtax_total_fen,
                ),
            ]
        )
    tax_profile = profile_as_of(
        session,
        org_id=request.org_id,
        as_of=component.start_date,
    )
    calculation = tax_result.to_dict()

    def persist(session, event: BusinessEvent, materialized: BusinessEventComponent) -> None:
        final_actual, final_review = tax_period_sources(
            session,
            organization,
            pending_event_id=event.id,
        )
        final_actual = [
            row
            for row in final_actual
            if component.start_date
            <= date.fromisoformat(row["tax_obligation_date"])
            <= component.end_date
        ]
        final_review = [
            row
            for row in final_review
            if component.start_date
            <= date.fromisoformat(row["tax_obligation_date"])
            <= component.end_date
        ]
        final_result = calculate_tax_period_from_sources(
            session,
            organization,
            component.start_date,
            component.end_date,
            request.posting_date,
            source_event_snapshots=final_actual,
            source_review_snapshots=final_review,
        )
        if (
            final_result.calculation_hash != tax_result.calculation_hash
            or final_result.calculation_hash_payload != tax_result.calculation_hash_payload
        ):
            raise ValueError("TAX_PERIOD_CALCULATION_STALE")
        final_calculation = final_result.to_dict()
        materialized.derived = dict(materialized.derived) | {
            "tax_period": final_calculation,
            "source_component_proofs": final_calculation["source_event_snapshots"],
            "source_review_proofs": final_calculation["source_review_snapshots"],
        }
        period = TaxPeriod(
            org_id=event.org_id,
            start_date=component.start_date,
            end_date=component.end_date,
            adjustment_posting_date=request.posting_date,
            rule_version=final_result.rule_version,
            calculation=final_calculation,
            calculation_hash=final_result.calculation_hash,
            calculation_hash_payload=final_result.calculation_hash_payload,
            filing_cycle_snapshot=tax_profile.filing_cycle,
            jurisdiction_snapshot=tax_profile.jurisdiction,
            urban_maintenance_rate_snapshot=Decimal(
                format(tax_profile.urban_maintenance_rate, ".5f")
            ),
            vat_rule_id=uuid.UUID(final_result.vat_rule_id),
            surtax_rule_id=uuid.UUID(final_result.surtax_rule_id),
            adjustment_event_id=event.id,
            component_id=materialized.id,
        )
        session.add(period)
        session.flush()
        for source in final_result.source_events:
            session.add(
                TaxPeriodSource(
                    org_id=event.org_id,
                    tax_period_id=period.id,
                    source_event_id=uuid.UUID(source["event_id"]),
                    source_component_id=uuid.UUID(source["component_id"]),
                    gross_fen=source["gross_fen"],
                    net_fen=source["net_fen"],
                    vat_fen=source["vat_fen"],
                    exemption_eligible=source["exemption_eligible"],
                )
            )
        session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="tax_adjustment_posted",
                details={
                    "tax_period_id": str(period.id),
                    "calculation_hash": final_result.calculation_hash,
                    "adjustment_posting_date": request.posting_date.isoformat(),
                    "component_id": str(materialized.id),
                },
            )
        )

    effects = [
        compiler.source_dependency(
            uuid.UUID(source["event_id"]),
            uuid.UUID(source["component_id"]),
            abs(int(source["gross_fen"])),
        )
        for source in tax_result.source_events
    ]
    effects.append(persist)
    return ComponentPostingPlan(
        key=component.key,
        kind=component.kind,
        facts=_component_payload(compiler, component),
        entries=entries,
        derived={
            "tax_period": calculation,
            "source_component_proofs": [
                {
                    "event_id": source["event_id"],
                    "component_id": source["component_id"],
                    "gross_fen": source["gross_fen"],
                    "net_fen": source["net_fen"],
                    "vat_fen": source["vat_fen"],
                    "exemption_eligible": source["exemption_eligible"],
                }
                for source in tax_result.source_events
            ],
            "source_review_proofs": tax_result.source_review_snapshots,
        },
        rule_version=tax_result.rule_version,
        effects=effects,
    )


def compile_enterprise_income_tax_assessment(compiler, component) -> ComponentPostingPlan:
    """Compile an accountant-confirmed quarterly accrual or reduction."""

    session = compiler.session
    request = compiler.request
    lock_income_tax(session, request.org_id)
    end_month = component.quarter * 3
    quarter_end = date(
        component.year,
        end_month,
        calendar.monthrange(component.year, end_month)[1],
    )
    quarter_start = date(component.year, end_month - 2, 1)
    if component.business_date != quarter_end:
        raise ValueError("ENTERPRISE_INCOME_TAX_BUSINESS_DATE_MISMATCH")
    if not quarter_start <= request.posting_date <= quarter_end:
        raise ValueError("ENTERPRISE_INCOME_TAX_POSTING_DATE_OUTSIDE_QUARTER")
    duplicate = session.scalar(
        select(EnterpriseIncomeTaxQuarterConfirmation.id).where(
            EnterpriseIncomeTaxQuarterConfirmation.org_id == request.org_id,
            EnterpriseIncomeTaxQuarterConfirmation.calendar_year == component.year,
            EnterpriseIncomeTaxQuarterConfirmation.calendar_quarter == component.quarter,
        )
    )
    if duplicate is not None:
        raise ValueError("ENTERPRISE_INCOME_TAX_QUARTER_ALREADY_CONFIRMED")
    if component.treatment == EnterpriseIncomeTaxTreatment.REDUCE:
        expense_balance = max(
            0,
            account_balance_fen(session, request.org_id, "enterprise_income_tax_expense"),
        )
        payable_balance = max(
            0,
            -account_balance_fen(session, request.org_id, "enterprise_income_tax_payable"),
        )
        if component.amount_fen > min(expense_balance, payable_balance):
            raise ValueError("ENTERPRISE_INCOME_TAX_REDUCTION_EXCEEDS_BALANCE")
    positive = component.treatment == EnterpriseIncomeTaxTreatment.ACCRUE
    entries = [
        Entry(
            account_role="enterprise_income_tax_expense",
            debit_fen=component.amount_fen if positive else 0,
            credit_fen=0 if positive else component.amount_fen,
        ),
        Entry(
            account_role="enterprise_income_tax_payable",
            credit_fen=component.amount_fen if positive else 0,
            debit_fen=0 if positive else component.amount_fen,
        ),
    ]
    calculation = {
        "org_id": str(request.org_id),
        "year": component.year,
        "quarter": component.quarter,
        "treatment": component.treatment,
        "amount_fen": component.amount_fen,
        "posting_date": request.posting_date.isoformat(),
        "event_id": str(compiler.event.id),
        "rule_version": ACCOUNTING_RULE_VERSION,
        "source_url": ACCOUNTING_RULE_SOURCE_URL,
    }
    calculation_payload, calculation_hash = _hash(calculation)
    facts = _component_payload(compiler, component)
    evidence_ids = _request_evidence(compiler, component)
    specialized = ConfirmEnterpriseIncomeTaxQuarterRequest(
        org_id=request.org_id,
        year=component.year,
        quarter=component.quarter,
        treatment=component.treatment,
        amount_fen=component.amount_fen,
        posting_date=request.posting_date,
        idempotency_key=request.idempotency_key,
        confirmation_note=component.confirmation_note,
        evidence_references=evidence_ids,
    )
    request_hash = digest(specialized.model_dump(mode="json"))
    evidence = [str(item) for item in evidence_ids]

    def persist(session, event: BusinessEvent, materialized: BusinessEventComponent) -> None:
        confirmation = EnterpriseIncomeTaxQuarterConfirmation(
            org_id=event.org_id,
            calendar_year=component.year,
            calendar_quarter=component.quarter,
            treatment=component.treatment,
            amount_fen=component.amount_fen,
            posting_date=request.posting_date,
            business_event_id=event.id,
            component_id=materialized.id,
            idempotency_key=(
                request.idempotency_key
                if len(request.components) == 1
                else f"{request.idempotency_key}:{component.key}"
            ),
            request_payload_hash=request_hash,
            calculation_payload=calculation_payload,
            calculation_hash=calculation_hash,
            confirmation_note=component.confirmation_note,
            evidence_references=evidence,
            execution_attribution_id=event.execution_attribution_id,
        )
        session.add(confirmation)
        session.flush()
        session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="enterprise_income_tax_quarter_confirmed",
                details={
                    "confirmation_id": str(confirmation.id),
                    "year": component.year,
                    "quarter": component.quarter,
                    "treatment": component.treatment,
                    "calculation_hash": calculation_hash,
                    "component_id": str(materialized.id),
                },
            )
        )

    return ComponentPostingPlan(
        key=component.key,
        kind=component.kind,
        facts=facts,
        entries=entries,
        derived=calculation
        | {
            "calculation_hash": calculation_hash,
            "recognized_tax_fen": component.amount_fen if positive else -component.amount_fen,
            "contribution_fen": component.amount_fen if positive else -component.amount_fen,
            "calendar_year": component.year,
            "calendar_quarter": component.quarter,
        },
        rule_version=ACCOUNTING_RULE_VERSION,
        effects=[persist],
    )


def compile_enterprise_income_tax_result(compiler, component) -> ComponentPostingPlan:
    """Compile one external CIT result as the delta from its active predecessor."""

    session = compiler.session
    request = compiler.request
    lock_income_tax(session, request.org_id)
    if component.business_date != component.declaration_date:
        raise ValueError("CIT_RESULT_BUSINESS_DATE_MISMATCH")
    evidence_ids = _request_evidence(compiler, component)
    preview_request = PreviewEnterpriseIncomeTaxResultRequest.model_validate(
        component.model_dump(
            exclude={
                "key",
                "kind",
                "business_date",
                "payment_date",
                "description",
                "depends_on",
                "account_selections",
                "calculation_hash",
                "evidence_references",
            }
        )
        | {
            "org_id": request.org_id,
            "posting_date": request.posting_date,
            "evidence_references": evidence_ids,
        }
    )
    service = EnterpriseIncomeTaxService(session)
    preview = service.preview(preview_request)
    if preview["status"] == "needs_information":
        from .component_service import MissingFacts

        raise MissingFacts(preview["missing_information"])
    if preview["status"] != "calculated":
        raise ValueError(preview["errors"][0])
    if not compiler.previewing and component.calculation_hash != preview["calculation_hash"]:
        raise ValueError("CIT_CALCULATION_STALE")
    calculation = preview["data"]
    delta = int(calculation["expense_adjustment_fen"])
    entries = (
        [
            Entry(
                account_role="enterprise_income_tax_expense",
                debit_fen=max(delta, 0),
                credit_fen=max(-delta, 0),
            ),
            Entry(
                account_role="enterprise_income_tax_payable",
                credit_fen=max(delta, 0),
                debit_fen=max(-delta, 0),
            ),
        ]
        if delta
        else []
    )
    specialized = ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
        preview_request.model_dump()
        | {
            "calculation_hash": component.calculation_hash,
            "idempotency_key": request.idempotency_key,
        }
    )
    input_facts = specialized.model_dump(mode="json")
    request_hash = digest(input_facts)

    def persist(session, event: BusinessEvent, materialized: BusinessEventComponent) -> None:
        row = EnterpriseIncomeTaxResult(
            org_id=event.org_id,
            calendar_year=component.year,
            calendar_quarter=component.quarter,
            revision=calculation["revision"],
            previous_result_id=component.previous_result_id,
            original_confirmation_id=component.original_confirmation_id,
            declaration_date=component.declaration_date,
            posting_date=request.posting_date,
            target_tax_fen=calculation["target_tax_fen"],
            contribution_fen=calculation["contribution_fen"],
            expense_adjustment_fen=delta,
            business_event_id=event.id,
            component_id=materialized.id,
            idempotency_key=(
                request.idempotency_key
                if len(request.components) == 1
                else f"{request.idempotency_key}:{component.key}"
            ),
            request_hash=request_hash,
            calculation_hash=component.calculation_hash,
            input_facts=input_facts,
            calculation=calculation,
            execution_attribution_id=event.execution_attribution_id,
        )
        session.add(row)
        session.flush()
        session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="enterprise_income_tax_result_confirmed",
                details={
                    "result_id": str(row.id),
                    "calculation_hash": row.calculation_hash,
                    "component_id": str(materialized.id),
                },
            )
        )

    effects = []
    if component.previous_result_id:
        previous = session.get(EnterpriseIncomeTaxResult, component.previous_result_id)
        if previous and previous.business_event_id and previous.component_id:
            effects.append(
                compiler.source_dependency(
                    previous.business_event_id,
                    previous.component_id,
                    abs(delta),
                )
            )
    elif component.original_confirmation_id:
        original = session.get(
            EnterpriseIncomeTaxQuarterConfirmation,
            component.original_confirmation_id,
        )
        if original and original.business_event_id and original.component_id:
            effects.append(
                compiler.source_dependency(
                    original.business_event_id,
                    original.component_id,
                    abs(delta),
                )
            )
    effects.append(persist)
    return ComponentPostingPlan(
        key=component.key,
        kind=component.kind,
        facts=_component_payload(compiler, component),
        entries=entries,
        derived=calculation
        | {
            "calculation_hash": preview["calculation_hash"],
            "calendar_year": component.year,
            "calendar_quarter": component.quarter,
            "expense_adjustment_fen": delta,
            "source_component_proofs": [
                value
                for value in (
                    {
                        "source_kind": "previous_result",
                        "source_id": str(component.previous_result_id),
                    }
                    if component.previous_result_id
                    else None,
                    {
                        "source_kind": "original_confirmation",
                        "source_id": str(component.original_confirmation_id),
                    }
                    if component.original_confirmation_id
                    else None,
                )
                if value is not None
            ],
        },
        rule_version=RULE["version"],
        effects=effects,
    )
