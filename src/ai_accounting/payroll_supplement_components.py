"""Historical contribution assessments compiled into the common posting plan."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from .component_service import MissingFacts
from .ledger import ComponentPostingPlan, Entry, OpenItemPlan
from .models import (
    BusinessEvent,
    BusinessEventComponent,
    PayrollBatch,
    PayrollContributionSupplement,
    PayrollContributionSupplementItem,
    PayrollEventLink,
    PayrollLine,
)
from .payroll import YearMonth
from .schemas import RecordPayrollContributionSupplementRequest


def compile_payroll_supplement(compiler, component) -> ComponentPostingPlan:
    service, session = compiler.common, compiler.session
    org_id = compiler.request.org_id
    request = RecordPayrollContributionSupplementRequest.model_validate(
        {
            **component.model_dump(
                exclude={
                    "key",
                    "kind",
                    "business_date",
                    "payment_date",
                    "description",
                    "depends_on",
                    "account_selections",
                    "source",
                }
            ),
            "org_id": org_id,
            "idempotency_key": compiler.request.idempotency_key,
            "posting_date": compiler.request.posting_date,
            "evidence_references": list(compiler.evidence_ids),
        }
    )
    employee = service._employee_for_org(org_id, request.employee_id)
    if employee is None:
        raise ValueError("EMPLOYEE_NOT_FOUND")
    period = YearMonth(int(request.contribution_period[:4]), int(request.contribution_period[5:]))
    policy = service._effective_payroll_policy(org_id, period.end_date)
    if policy is None:
        raise MissingFacts([f"components.{component.key}.contribution_policy_version"])
    contribution_policy, _, _ = service._calculator_policies(policy)
    policy_keys = {(str(rule.base_kind), rule.code) for rule in contribution_policy.rules}
    item_keys = {(item.contribution_group.value, item.insurance_kind) for item in request.items}
    if invalid := sorted(item_keys - policy_keys):
        raise ValueError(
            "CONTRIBUTION_SUPPLEMENT_KIND_NOT_IN_POLICY:"
            + ",".join(f"{group}:{kind}" for group, kind in invalid)
        )
    profile = service._effective_profile(employee.id, period.end_date)
    if profile is None:
        raise MissingFacts([f"components.{component.key}.employee_payroll_profile_version"])

    source_component_id = None
    local_key = component.source.component_key if component.source else None
    if component.source:
        if local_key:
            source = compiler.plans[local_key]
        else:
            source = session.get(BusinessEventComponent, component.source.component_id)
            if source is None or source.org_id != org_id:
                raise ValueError("CONTRIBUTION_SUPPLEMENT_PAYROLL_SOURCE_INVALID")
            source_component_id = source.id
            source_event = session.get(BusinessEvent, source.event_id)
            if source_event.status != "posted" or source_event.posting_date > request.posting_date:
                raise ValueError("SOURCE_COMPONENT_NOT_ACTIVE_OR_FUTURE")
        if source.kind != "payroll_accrual":
            raise ValueError("CONTRIBUTION_SUPPLEMENT_PAYROLL_SOURCE_INVALID")
        batch_id = uuid.UUID(source.derived["payroll_batch_id"])
        candidates = [session.get(PayrollBatch, batch_id)]
    else:
        candidates = list(
            session.scalars(
                select(PayrollBatch)
                .join(PayrollLine, PayrollLine.payroll_batch_id == PayrollBatch.id)
                .where(
                    PayrollBatch.org_id == org_id,
                    PayrollBatch.batch_kind == "regular",
                    PayrollBatch.payroll_period == request.contribution_period,
                    PayrollBatch.status == "posted",
                    PayrollLine.employee_id == employee.id,
                )
                .order_by(PayrollBatch.id)
                .with_for_update()
            )
        )
    if len(candidates) != 1:
        raise MissingFacts([f"components.{component.key}.unique_source_payroll_batch"])
    batch = candidates[0]
    if (
        batch is None
        or batch.org_id != org_id
        or batch.batch_kind != "regular"
        or batch.payroll_period != request.contribution_period
        or (not local_key and batch.status != "posted")
        or session.scalar(
            select(PayrollLine.id).where(
                PayrollLine.payroll_batch_id == batch.id, PayrollLine.employee_id == employee.id
            )
        )
        is None
    ):
        raise ValueError("CONTRIBUTION_SUPPLEMENT_PAYROLL_SOURCE_INVALID")
    if not local_key and source_component_id is None:
        source_component_id = session.scalar(
            select(PayrollEventLink.component_id).where(
                PayrollEventLink.payroll_batch_id == batch.id,
                PayrollEventLink.event_id == batch.business_event_id,
                PayrollEventLink.link_kind == "payroll_accrual",
            )
        )
    if session.scalar(
        select(PayrollContributionSupplement.id).where(
            PayrollContributionSupplement.org_id == org_id,
            PayrollContributionSupplement.employee_id == employee.id,
            PayrollContributionSupplement.assessment_reference == request.assessment_reference,
        )
    ) or any(
        p.kind == component.kind
        and p.facts["employee_id"] == str(employee.id)
        and p.facts["assessment_reference"] == request.assessment_reference
        for p in compiler.plans.values()
    ):
        raise ValueError("CONTRIBUTION_SUPPLEMENT_ASSESSMENT_ALREADY_RECORDED")

    entries, obligations = [], []
    targets = service._payment_targets(policy.parameters)
    for item in request.items:
        social = item.contribution_group.value == "social_insurance"
        employer_role = "employer_social_payable" if social else "employer_housing_fund_payable"
        withheld_role = (
            "withheld_employee_social_payable"
            if social
            else "withheld_employee_housing_fund_payable"
        )
        employer_category = "employer_social" if social else "employer_housing"
        withheld_category = "withheld_employee_social" if social else "withheld_employee_housing"
        target = targets["social_insurance" if social else "housing_fund"]
        agency = service._agency_counterparty(org_id, target)
        employer = item.employer_amount_fen + (
            item.employee_amount_fen if item.employee_amount_treatment == "employer_borne" else 0
        )
        employee_amount = (
            item.employee_amount_fen
            if item.employee_amount_treatment == "employee_receivable"
            else 0
        )
        for amount, role, category in (
            (employer, employer_role, employer_category),
            (employee_amount, withheld_role, withheld_category),
        ):
            if not amount:
                continue
            entries.extend(
                [
                    Entry(
                        account_role=(
                            profile.expense_role
                            if category == employer_category
                            else "employee_receivable"
                        ),
                        debit_fen=amount,
                        counterparty_id=employee.counterparty_id,
                    ),
                    Entry(account_role=role, credit_fen=amount, counterparty_id=agency.id),
                ]
            )
            obligations.append(
                OpenItemPlan(
                    key=f"{category}.{item.insurance_kind}",
                    counterparty_id=agency.id,
                    item_type="payable",
                    original_amount_fen=amount,
                    due_date=request.due_date,
                    payable_category=category,
                    payable_agency_code=target["agency_code"],
                    insurance_kind=item.insurance_kind,
                    account_role=role,
                )
            )
        if employee_amount:
            obligations.append(
                OpenItemPlan(
                    key=f"employee_receivable.{item.contribution_group.value}.{item.insurance_kind}",
                    counterparty_id=employee.counterparty_id,
                    item_type="receivable",
                    original_amount_fen=employee_amount,
                    due_date=request.due_date,
                    account_role="employee_receivable",
                )
            )

    supplement_id = uuid.uuid4()

    def apply(session, event, persisted):
        supplement = PayrollContributionSupplement(
            id=supplement_id,
            org_id=org_id,
            event_id=event.id,
            component_id=persisted.id,
            employee_id=employee.id,
            source_payroll_batch_id=batch.id,
            contribution_period=request.contribution_period,
            assessment_reference=request.assessment_reference,
            reason_code=request.reason_code,
            reason_description=request.reason_description,
        )
        session.add(supplement)
        session.flush()
        session.add_all(
            PayrollContributionSupplementItem(
                org_id=org_id, supplement_id=supplement.id, **item.model_dump(mode="json")
            )
            for item in request.items
        )
        session.add(
            PayrollEventLink(
                org_id=org_id,
                event_id=event.id,
                component_id=persisted.id,
                payroll_batch_id=batch.id,
                link_kind="contribution_supplement",
            )
        )
        parent_component = source_component_id
        parent_event = batch.business_event_id
        if local_key:
            parent = session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == event.id,
                    BusinessEventComponent.key == local_key,
                )
            )
            parent_component, parent_event = parent.id, event.id
        compiler.source_dependency(
            parent_event,
            parent_component,
            sum(item.employee_amount_fen + item.employer_amount_fen for item in request.items),
        )(session, event, persisted)

    return ComponentPostingPlan(
        key=component.key,
        kind=component.kind,
        facts=component.model_dump(mode="json"),
        entries=entries,
        open_items=obligations,
        effects=[apply],
        rule_version=policy.version,
        derived={
            "payroll_batch_id": str(batch.id),
            "supplement_id": str(supplement_id),
            "policy_version_id": str(policy.id),
        },
    )
