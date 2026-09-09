"""Salary obligations, explicit deductions and their normalized provenance."""

from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import func, select

from .ledger import ComponentPostingPlan, Entry, SettlementPlan
from .models import (
    Account,
    BusinessEventComponent,
    OpenItem,
    PayrollEventLink,
    PayrollWithholdingEntitlement,
    PayrollWithholdingPaymentAllocation,
)


def validate_salary_settlement_plans(session, org_id, plans) -> None:
    """Validate repeated salary components against one projected event state."""

    salary_plans = [plan for plan in plans if plan.kind == "salary_settlement"]
    if not salary_plans:
        return

    all_plans = {plan.key: plan for plan in plans}
    gross_by_item: dict[str, int] = {}
    line_by_item: dict[str, uuid.UUID] = {}
    planned_by_entitlement: dict[uuid.UUID, int] = {}
    for plan in salary_plans:
        for allocation in plan.derived["allocations"]:
            item_id = (
                f"id:{allocation['open_item_id']}"
                if allocation.get("open_item_id")
                else (
                    f"local:{allocation['source_component_key']}:"
                    f"{allocation['source_open_item_key']}"
                )
            )
            line_id = uuid.UUID(allocation["payroll_line_id"])
            previous_line = line_by_item.setdefault(item_id, line_id)
            if previous_line != line_id:
                raise ValueError("salary open item has inconsistent payroll-line provenance")
            settlement = next(
                item
                for item in plan.settlements
                if (
                    item_id == f"id:{item.open_item_id}"
                    or item_id
                    == f"local:{item.source_component_key}:{item.source_open_item_key}"
                )
            )
            gross_by_item[item_id] = gross_by_item.get(item_id, 0) + settlement.amount_fen
        for allocation in plan.derived["withholding_payment_allocations"]:
            entitlement_id = uuid.UUID(allocation["entitlement_id"])
            planned_by_entitlement[entitlement_id] = (
                planned_by_entitlement.get(entitlement_id, 0)
                + int(allocation["amount_fen"])
            )

    persisted_ids = [uuid.UUID(key[3:]) for key in gross_by_item if key.startswith("id:")]
    items = {
        item.id: item
        for item in session.scalars(
            select(OpenItem)
            .where(OpenItem.org_id == org_id, OpenItem.id.in_(persisted_ids))
            .order_by(OpenItem.id)
            .with_for_update()
        )
    }
    if len(items) != len(persisted_ids):
        raise ValueError("salary settlement source disappeared during whole-plan validation")
    final_line_ids: set[uuid.UUID] = set()
    for item_id, requested in gross_by_item.items():
        if item_id.startswith("id:"):
            item = items[uuid.UUID(item_id[3:])]
            available = item.original_amount_fen - item.settled_amount_fen
        else:
            _, component_key, open_item_key = item_id.split(":", 2)
            source_plan = all_plans[component_key]
            item_plan = next(
                item for item in source_plan.open_items if item.key == open_item_key
            )
            available = item_plan.original_amount_fen
        if requested > available:
            raise ValueError(
                f"allocation exceeds open amount for {item_id}: "
                f"available={available}, requested={requested}"
            )
        if requested == available:
            final_line_ids.add(line_by_item[item_id])

    line_ids = set(line_by_item.values())
    entitlements = list(
        session.scalars(
            select(PayrollWithholdingEntitlement)
            .where(
                PayrollWithholdingEntitlement.org_id == org_id,
                PayrollWithholdingEntitlement.payroll_line_id.in_(line_ids),
            )
            .order_by(
                PayrollWithholdingEntitlement.payroll_line_id,
                PayrollWithholdingEntitlement.contribution_group,
                PayrollWithholdingEntitlement.insurance_kind,
            )
            .with_for_update()
        )
    )
    entitlement_ids = [item.id for item in entitlements]
    paid_by_entitlement = {
        entitlement_id: int(amount)
        for entitlement_id, amount in session.execute(
            select(
                PayrollWithholdingPaymentAllocation.entitlement_id,
                func.coalesce(func.sum(PayrollWithholdingPaymentAllocation.amount_fen), 0),
            )
            .where(
                PayrollWithholdingPaymentAllocation.org_id == org_id,
                PayrollWithholdingPaymentAllocation.entitlement_id.in_(entitlement_ids),
                PayrollWithholdingPaymentAllocation.reversed.is_(False),
            )
            .group_by(PayrollWithholdingPaymentAllocation.entitlement_id)
        )
    }
    labels = {
        "employee_social_insurance": "employee social insurance",
        "employee_housing_fund": "employee housing fund",
        "individual_income_tax": "individual income tax",
    }
    for entitlement in entitlements:
        projected = paid_by_entitlement.get(entitlement.id, 0) + planned_by_entitlement.get(
            entitlement.id, 0
        )
        label = labels[entitlement.contribution_group]
        if projected > entitlement.amount_fen:
            raise ValueError(f"{label} withholding exceeds the payroll-line entitlement")
        if entitlement.payroll_line_id in final_line_ids and projected != entitlement.amount_fen:
            raise ValueError(
                "final salary payment must explicitly account for every "
                "payroll-line withholding"
            )


def compile_salary_settlement(
    session,
    org_id,
    component,
    *,
    source_plans=None,
    pending_event_id=None,
) -> ComponentPostingPlan:
    from .service import FinanceService

    service = FinanceService(session)
    local_allocations = [a for a in component.allocations if a.source_component_key]
    if local_allocations:
        if len(local_allocations) != len(component.allocations):
            raise ValueError("salary settlement cannot mix posted and local sources")
        return _compile_local_salary_settlement(
            session, org_id, component, source_plans or {}, pending_event_id, service
        )
    derived = service.derive_salary_settlement(org_id, component)
    derived["salary_withholding_allocations"] = derived["allocations"]
    allocation_details = {
        uuid.UUID(row["open_item_id"]): row for row in derived["allocations"]
    }
    cash_flow_parts = []
    for allocation in component.allocations:
        details = allocation_details[allocation.open_item_id]
        cash_amount = (
            allocation.amount_fen
            - sum(details["employee_social_insurance_items"].values())
            - sum(details["employee_housing_fund_items"].values())
            - details["individual_income_tax_fen"]
            - details["actual_salary_deduction_fen"]
        )
        if cash_amount:
            cash_flow_parts.append(
                {
                    "category": "cash_flow_4",
                    "amount_fen": cash_amount,
                    "source": allocation.model_dump(mode="json", exclude={"amount_fen"}),
                }
            )
    derived["cash_flow_parts"] = cash_flow_parts
    derived["payment_date"] = component.payment_date.isoformat()
    entries, settlements = [], []
    for allocation in component.allocations:
        item = session.scalar(
            select(OpenItem)
            .where(
                OpenItem.org_id == org_id,
                OpenItem.id == allocation.open_item_id,
            )
            .with_for_update()
        )
        entries.append(
            Entry(
                account_code=session.get(Account, item.account_id).code,
                debit_fen=allocation.amount_fen,
                counterparty_id=item.counterparty_id,
            )
        )
        settlements.append(
            SettlementPlan(
                amount_fen=allocation.amount_fen,
                purpose="salary_settlement",
                expected_item_type="payable",
                counterparty_id=item.counterparty_id,
                open_item_id=item.id,
            )
        )
    open_items = service._salary_withholding_open_item_plans(
        org_id,
        component.payment_date,
        derived,
    )
    roles = {
        "withheld_employee_social": "withheld_employee_social_payable",
        "withheld_employee_housing": "withheld_employee_housing_fund_payable",
        "individual_income_tax": "individual_income_tax_payable",
    }
    accounts = {}
    named_items = []
    for item in open_items:
        key = f"{item.payable_category}.{item.insurance_kind or 'tax'}"
        named_items.append(replace(item, key=key))
        role = roles[item.payable_category]
        accounts[key] = role
        entries.append(
            Entry(
                account_role=role,
                credit_fen=item.original_amount_fen,
                counterparty_id=item.counterparty_id,
            )
        )
    for role, amount in derived["actual_salary_deduction_by_expense_role"].items():
        if amount:
            entries.append(Entry(account_role=role, credit_fen=amount))
    derived["open_item_accounts"] = accounts

    def apply(session, event, persisted):
        service._record_payroll_withholding_allocations(event, derived, component_id=persisted.id)
        for allocation in component.allocations:
            source = next(
                row
                for row in derived["allocations"]
                if row["open_item_id"] == str(allocation.open_item_id)
            )
            session.add(
                PayrollEventLink(
                    org_id=org_id,
                    event_id=event.id,
                    component_id=persisted.id,
                    payroll_batch_id=uuid.UUID(source["payroll_batch_id"]),
                    source_open_item_id=allocation.open_item_id,
                    link_kind="salary_payment",
                )
            )

    return ComponentPostingPlan(
        key=component.key,
        kind=component.kind,
        facts=component.model_dump(mode="json"),
        entries=entries,
        derived=derived,
        rule_version="salary-settlement-components-v2",
        open_items=named_items,
        settlements=settlements,
        effects=[apply],
    )


def _compile_local_salary_settlement(
    session, org_id, component, source_plans, pending_event_id, service
) -> ComponentPostingPlan:
    def ref(item):
        return (item.source_component_key, item.source_open_item_key)

    allocation_by_source = {ref(item): item for item in component.allocations}
    withholding_by_source = {ref(item): item for item in component.withholding_allocations}
    deduction_by_source = {
        ref(item): item.amount_fen for item in component.actual_deduction_allocations
    }
    if len(allocation_by_source) != len(component.allocations):
        raise ValueError("salary payment cannot allocate an open item more than once")
    if set(allocation_by_source) != set(withholding_by_source):
        raise ValueError("salary payment needs explicit withholdings for every salary allocation")
    if not set(deduction_by_source).issubset(allocation_by_source):
        raise ValueError("salary actual deduction must belong to a salary allocation")

    batch_ids: set[str] = set()
    cash_total = social_total = housing_total = tax_total = actual_total = 0
    serialised = []
    withholding_rows = []
    actual_rows = []
    actual_by_role = {}
    entries = []
    settlements = []
    for source_ref, allocation in allocation_by_source.items():
        source_key, open_item_key = source_ref
        plan = source_plans.get(source_key)
        if plan is None or plan.kind != "payroll_accrual":
            raise ValueError("salary open item does not originate from a payroll accrual")
        item_plan = next((item for item in plan.open_items if item.key == open_item_key), None)
        source = next(
            (
                item
                for item in plan.derived.get("salary_sources", [])
                if item["open_item_key"] == open_item_key
            ),
            None,
        )
        if item_plan is None or source is None:
            raise ValueError("salary open item has no matching payroll line")
        current_batch = plan.derived["payroll_batch_id"]
        batch_ids.add(current_batch)
        if allocation.amount_fen > item_plan.original_amount_fen:
            raise ValueError("allocation exceeds local salary amount")
        entitlements = [
            session.get(PayrollWithholdingEntitlement, uuid.UUID(item["id"]))
            for item in source["entitlements"]
        ]
        if any(item is None for item in entitlements):
            raise ValueError("salary payroll entitlement is not available")
        supplied = withholding_by_source[source_ref]
        final = allocation.amount_fen == item_plan.original_amount_fen
        social, social_rows = service._validated_withholding_components(
            supplied.employee_social_insurance_items,
            entitlements,
            "employee_social_insurance",
            "employee social insurance",
            final_payment=final,
        )
        housing, housing_rows = service._validated_withholding_components(
            supplied.employee_housing_fund_items,
            entitlements,
            "employee_housing_fund",
            "employee housing fund",
            final_payment=final,
        )
        tax_parts, tax_rows = service._validated_withholding_components(
            {"individual_income_tax": supplied.individual_income_tax_fen},
            entitlements,
            "individual_income_tax",
            "individual income tax",
            final_payment=final,
        )
        tax = sum(tax_parts.values())
        deduction = int(deduction_by_source.get(source_ref, 0))
        withheld = sum(social.values()) + sum(housing.values()) + tax
        if withheld + deduction > allocation.amount_fen:
            raise ValueError(
                "salary withholdings and actual deduction exceed the allocated gross salary"
            )
        cash_total += allocation.amount_fen - withheld - deduction
        social_total += sum(social.values())
        housing_total += sum(housing.values())
        tax_total += tax
        actual_total += deduction
        line_id = source["payroll_line_id"]
        allocation_json = {
            "source_component_key": source_key,
            "source_open_item_key": open_item_key,
            "open_item_id": None,
            "payroll_batch_id": current_batch,
            "payroll_line_id": line_id,
            "employee_social_insurance_items": social,
            "employee_housing_fund_items": housing,
            "individual_income_tax_fen": tax,
            "actual_salary_deduction_fen": deduction,
            "expense_role": source["expense_role"],
        }
        serialised.append(allocation_json)
        withholding_rows.extend([*social_rows, *housing_rows, *tax_rows])
        if deduction:
            actual_by_role[source["expense_role"]] = (
                actual_by_role.get(source["expense_role"], 0) + deduction
            )
            actual_rows.append(
                {
                    "open_item_id": None,
                    "payroll_line_id": line_id,
                    "amount_fen": deduction,
                    "expense_role": source["expense_role"],
                }
            )
        entries.append(
            Entry(
                account_role="employee_salary_payable",
                debit_fen=allocation.amount_fen,
                counterparty_id=item_plan.counterparty_id,
            )
        )
        settlements.append(
            SettlementPlan(
                amount_fen=allocation.amount_fen,
                purpose="salary_settlement",
                expected_item_type="payable",
                counterparty_id=item_plan.counterparty_id,
                source_component_key=source_key,
                source_open_item_key=open_item_key,
            )
        )
    if not batch_ids or cash_total != component.amount_fen:
        raise ValueError(
            "salary cash payment must equal gross allocations less explicit "
            "withholdings and actual salary deductions"
        )
    derived = {
        "payroll_batch_ids": sorted(batch_ids),
        "gross_salary_fen": sum(item.amount_fen for item in component.allocations),
        "employee_social_insurance_fen": social_total,
        "employee_housing_fund_fen": housing_total,
        "individual_income_tax_fen": tax_total,
        "actual_salary_deduction_fen": actual_total,
        "actual_salary_deduction_by_expense_role": actual_by_role,
        "actual_salary_deduction_allocations": actual_rows,
        "allocations": serialised,
        "salary_withholding_allocations": serialised,
        "withholding_payment_allocations": withholding_rows,
        "payroll_line_ids": sorted({row["payroll_line_id"] for row in withholding_rows}),
        "cash_flow_category": "cash_flow_4",
        "payment_date": component.payment_date.isoformat(),
    }
    open_items = service._salary_withholding_open_item_plans(
        org_id, component.payment_date, derived
    )
    roles = {
        "withheld_employee_social": "withheld_employee_social_payable",
        "withheld_employee_housing": "withheld_employee_housing_fund_payable",
        "individual_income_tax": "individual_income_tax_payable",
    }
    named_items = []
    for item in open_items:
        key = f"{item.payable_category}.{item.insurance_kind or 'tax'}"
        named_items.append(replace(item, key=key))
        entries.append(
            Entry(
                account_role=roles[item.payable_category],
                credit_fen=item.original_amount_fen,
                counterparty_id=item.counterparty_id,
            )
        )
    for role, amount in actual_by_role.items():
        if amount:
            entries.append(Entry(account_role=role, credit_fen=amount))

    def apply(session, event, persisted):
        materialized = {
            (row.source_component_id, row.component_key): row
            for row in session.scalars(
                select(OpenItem).where(OpenItem.source_event_id == event.id)
            )
        }
        components = {
            row.key: row
            for row in session.scalars(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == event.id
                )
            )
        }
        for row in derived["allocations"]:
            item = materialized[
                (components[row["source_component_key"]].id, row["source_open_item_key"])
            ]
            row["open_item_id"] = str(item.id)
        for row in derived["actual_salary_deduction_allocations"]:
            source_row = next(
                a
                for a in derived["allocations"]
                if a["payroll_line_id"] == row["payroll_line_id"]
            )
            source_item = materialized[
                (
                    components[source_row["source_component_key"]].id,
                    source_row["source_open_item_key"],
                )
            ]
            row["open_item_id"] = str(source_item.id)
        service._record_payroll_withholding_allocations(event, derived, component_id=persisted.id)
        for row in derived["allocations"]:
            session.add(
                PayrollEventLink(
                    org_id=org_id,
                    event_id=event.id,
                    component_id=persisted.id,
                    payroll_batch_id=uuid.UUID(row["payroll_batch_id"]),
                    source_open_item_id=uuid.UUID(row["open_item_id"]),
                    link_kind="salary_payment",
                )
            )

    return ComponentPostingPlan(
        key=component.key,
        kind=component.kind,
        facts=component.model_dump(mode="json"),
        entries=entries,
        derived=derived,
        rule_version="salary-settlement-components-v2",
        open_items=named_items,
        settlements=settlements,
        effects=[apply],
    )
