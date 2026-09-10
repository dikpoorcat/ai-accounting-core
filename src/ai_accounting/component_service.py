"""Compile typed business components and settle their funds once, atomically."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .accounting_periods import canonical_sha256
from .business_metadata import metadata_projection, save_initial_metadata, validate_metadata
from .coa import (
    account_business_class,
    get_account_by_code,
    get_account_by_role,
    get_business_class_template,
)
from .component_schemas import ConfigureAccountRequest, RecordEventRequest
from .fact_dates import recognition_date, recognition_projection
from .fact_requirements import AccountingFactError, AccountingFactIssue, recognition_issue
from .ledger import (
    CashFlowPlan,
    ComponentPostingPlan,
    Entry,
    OpenItemPlan,
    SettlementPlan,
    assert_period_open,
    build_business_event,
    commit_posting_plan,
    funds_posting_plan,
    posting_period_error_code,
)
from .models import (
    Account,
    AuditLog,
    BusinessEvent,
    BusinessEventComponent,
    BusinessEventDependency,
    Counterparty,
    DeferredOutputVatTransfer,
    Invoice,
    OpenItem,
    Organization,
    TaxPeriod,
    Voucher,
    ZeroTaxPeriodConfirmation,
)
from .schemas import FinanceResult, ResultStatus
from .tax import active_tax_rule, calculate_tax_period, split_tax_inclusive
from .tax_accounts import deferred_vat_transfer_entries, tax_settlement_entries

RULE_VERSION = "business-components-v3"
EXPENSE_CLASSES = frozenset(
    {
        "service_cost",
        "sales_expense",
        "general_expense",
        "finance_expense",
        "labor_service_cost",
        "social_insurance_late_fee_expense",
        "borrowing_interest_expense",
    }
)
PAYABLE_ROLES = {
    "salary": "employee_salary_payable",
    "employer_social": "employer_social_payable",
    "withheld_employee_social": "withheld_employee_social_payable",
    "employer_housing": "employer_housing_fund_payable",
    "withheld_employee_housing": "withheld_employee_housing_fund_payable",
    "individual_income_tax": "individual_income_tax_payable",
    "labor_remuneration": "labor_remuneration_payable",
    "labor_individual_income_tax": "individual_income_tax_payable",
    "pass_through": "pass_through_payable",
}


class MissingFacts(ValueError):
    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__("COMPONENT_NEEDS_INFORMATION")


def payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class ComponentService:
    def __init__(self, session: Session):
        self.session = session
        # Shared identity/evidence/bank checks have no event-template dispatch.
        from .service import FinanceService

        self.common = FinanceService(session)
        self.plans: dict[str, ComponentPostingPlan] = {}
        self.request: RecordEventRequest
        self.previewing = False

    def prepare_request(self, request):
        """Resolve explicit business references and reuse authoritative fund dates."""
        from pydantic import BaseModel

        from .component_schemas import ObligationAllocation, SourceReference
        from .schemas import SalaryActualDeductionAllocation, SalaryWithholdingAllocation

        obligation_types = (
            ObligationAllocation,
            SalaryActualDeductionAllocation,
            SalaryWithholdingAllocation,
        )

        def resolve(value):
            if isinstance(value, (SourceReference, *obligation_types)):
                obligation = isinstance(value, obligation_types)
                event_key = value.source_event_key if obligation else value.event_key
                if event_key:
                    component_key = (
                        value.source_component_key if obligation else value.component_key
                    )
                    source = self.session.scalar(
                        select(BusinessEventComponent)
                        .join(
                            BusinessEvent,
                            BusinessEvent.id == BusinessEventComponent.event_id,
                        )
                        .where(
                            BusinessEvent.org_id == request.org_id,
                            BusinessEvent.idempotency_key == event_key,
                            BusinessEventComponent.key == component_key,
                        )
                    )
                    if source is None:
                        raise ValueError("SOURCE_BUSINESS_REFERENCE_NOT_FOUND")
                    if obligation:
                        item = self.session.scalar(
                            select(OpenItem).where(
                                OpenItem.source_component_id == source.id,
                                OpenItem.component_key == value.source_open_item_key,
                            )
                        )
                        if item is None:
                            raise ValueError("SOURCE_OBLIGATION_REFERENCE_NOT_FOUND")
                        return value.model_copy(
                            update={
                                "open_item_id": item.id,
                                "source_component_key": None,
                                "source_event_key": None,
                                "source_open_item_key": "primary",
                            }
                        )
                    return value.model_copy(
                        update={"component_id": source.id, "component_key": None, "event_key": None}
                    )
                if obligation and value.open_item_id is not None:
                    # Once the UUID uniquely identifies the obligation, the
                    # business-key selector is redundant in the normalized facts.
                    return value.model_copy(update={"source_open_item_key": "primary"})
            if isinstance(value, BaseModel):
                return value.model_copy(
                    update={
                        name: resolve(getattr(value, name))
                        for name in type(value).model_fields
                        if name != "metadata"
                    }
                )
            if isinstance(value, list):
                return [resolve(item) for item in value]
            return value

        resolved = resolve(request)
        for c in resolved.components:
            validate_metadata(self.session, request.org_id, c.metadata)
            if (
                c.kind == "pass_through"
                and c.recognition_basis == "credit"
                and not (c.evidence_references or resolved.evidence_references)
            ):
                raise AccountingFactError(
                    AccountingFactIssue(
                        code="PASS_THROUGH_CREDIT_EVIDENCE_REQUIRED",
                        kind="missing_accounting_fact",
                        fields=[f"components.{c.key}.evidence_references"],
                        message="先查阅公司说明和原始依据；须有确认收款债权及转付义务已成立的证据。",
                    ),
                    [f"components.{c.key}.evidence_references"],
                )
            dates = {
                f.payment_date
                for f in resolved.funds
                if any(a.component_key == c.key for a in f.allocations)
            }
            if any(day > request.posting_date for day in dates):
                raise AccountingFactError(
                    AccountingFactIssue(
                        code="FUNDS_PAYMENT_DATE_IN_FUTURE",
                        kind="conflicting_accounting_facts",
                        fields=["funds.payment_date", "posting_date"],
                        actual_values={
                            "payment_dates": sorted(dates),
                            "posting_date": request.posting_date,
                        },
                        expected={"payment_not_after": request.posting_date},
                        context={"component_key": c.key},
                        message="核对本公司资金项的真实收付款日期和记账日；不得用外部申报日或月末替代，不得为通过校验改写真实资金日期。",
                    )
                )
            if c.payment_date is None and len(dates) == 1:
                c.payment_date = next(iter(dates))
            if c.recognition_period:
                if c.recognition_date > request.posting_date:
                    raise recognition_issue(
                        code="RECOGNITION_PERIOD_IN_FUTURE",
                        business_date=c.business_date,
                        recognition_period=c.recognition_period,
                        posting_date=request.posting_date,
                        component_key=c.key,
                    )
                if (c.kind == "expense" and c.payment_basis == "immediate") or (
                    c.kind == "refundable_deposit" and not c.advanced_by
                ):
                    raise ValueError("MONTHLY_RECOGNITION_REQUIRES_NONCASH_BUSINESS")
            elif c.business_date is None:
                if getattr(c, "fulfillment_date", None):
                    c.business_date = c.fulfillment_date
                elif len(dates) == 1:
                    c.business_date = next(iter(dates))
                elif dates:
                    # This is the explicit voucher recognition date; actual
                    # movements remain individually dated in the fund items.
                    c.business_date = request.posting_date
                elif c.kind in {
                    "payable_settlement",
                    "receivable_settlement",
                    "supplier_advance_application",
                    "project_cost_expense",
                }:
                    c.business_date = request.posting_date
                else:
                    if c.supports_monthly_recognition:
                        raise recognition_issue(
                            code="ACCOUNTING_RECOGNITION_REQUIRED",
                            business_date=None,
                            recognition_period=None,
                            posting_date=request.posting_date,
                            component_key=c.key,
                            missing=True,
                        )
                    raise MissingFacts([f"components.{c.key}.business_date"])
        return resolved

    @staticmethod
    def accounting_request(request):
        values = request.model_dump(
            mode="json", exclude={"description": True, "components": {"__all__": {"metadata"}}}
        )
        values["components"] = [component.accounting_facts() for component in request.components]
        return values

    def record(self, request: RecordEventRequest) -> FinanceResult:
        self.previewing = False
        self.request = request
        self.plans = {}
        try:
            with self.session.begin_nested():
                organization = self.session.scalar(
                    select(Organization)
                    .where(
                        Organization.id == request.org_id,
                    )
                    .with_for_update()
                )
                if organization is None:
                    raise ValueError("ORGANIZATION_NOT_FOUND")
                request = self.prepare_request(request)
                self.request = request
                digest = payload_hash(self.accounting_request(request))
                existing = self.session.scalar(
                    select(BusinessEvent).where(
                        BusinessEvent.org_id == request.org_id,
                        BusinessEvent.idempotency_key == request.idempotency_key,
                    )
                )
                if existing is not None:
                    if existing.request_payload_hash != digest:
                        raise ValueError("IDEMPOTENCY_KEY_PAYLOAD_MISMATCH")
                    result = self.result(existing)
                    result.data["idempotent_replay"] = True
                    return result
                assert_period_open(self.session, request.org_id, request.posting_date)
                evidence_ids = sorted(
                    set(request.evidence_references).union(
                        *(set(c.evidence_references) for c in request.components)
                    ),
                    key=str,
                )
                if not evidence_ids:
                    raise MissingFacts(["evidence_references"])
                event = build_business_event(
                    self.session,
                    org_id=request.org_id,
                    idempotency_key=request.idempotency_key,
                    request_payload_hash=digest,
                    event_type="composite",
                    status="draft",
                    description=request.description,
                    facts=self.accounting_request(request),
                    business_date=min(c.recognition_date for c in request.components),
                    posting_date=request.posting_date,
                    rule_version=RULE_VERSION,
                    rule_trace=[{"stage": "component_compilation", "version": RULE_VERSION}],
                )
                self.session.add(event)
                self.session.flush()
                self.common._attach_evidence(event, evidence_ids)
                self.event = event
                self.evidence_ids = set(evidence_ids)
                self._compile_plans(organization)
                commit_posting_plan(
                    self.session,
                    event=event,
                    components=list(self.plans.values()),
                    posting_date=request.posting_date,
                    description=request.description,
                )
                for component in request.components:
                    save_initial_metadata(self.session, event, component)
                self.session.flush()
                self.session.expire(event, ["vouchers"])
                return self.result(event)
        except AccountingFactError as exc:
            return FinanceResult(**exc.result())
        except MissingFacts as exc:
            return FinanceResult(
                status=ResultStatus.NEEDS_INFORMATION, missing_information=exc.paths
            )
        except (ValueError, LookupError) as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[str(exc)])
        except IntegrityError:
            return FinanceResult(
                status=ResultStatus.REJECTED, errors=["COMPONENT_DATABASE_CONSTRAINT_VIOLATION"]
            )

    def preview(self, request: RecordEventRequest) -> FinanceResult:
        """Compile the exact typed graph while rolling back its temporary draft state."""

        self.previewing = True
        self.request = request
        self.plans = {}
        transaction = self.session.begin_nested()
        try:
            organization = self.session.get(Organization, request.org_id)
            if organization is None:
                raise ValueError("ORGANIZATION_NOT_FOUND")
            request = self.prepare_request(request)
            self.request = request
            if error := posting_period_error_code(
                self.session, request.org_id, request.posting_date
            ):
                raise ValueError(error)
            evidence_ids = sorted(
                set(request.evidence_references).union(
                    *(set(component.evidence_references) for component in request.components)
                ),
                key=str,
            )
            if not evidence_ids:
                raise MissingFacts(["evidence_references"])
            event = build_business_event(
                self.session,
                org_id=request.org_id,
                idempotency_key=f"preview:{uuid.uuid4()}",
                request_payload_hash=payload_hash(self.accounting_request(request)),
                event_type="composite",
                status="draft",
                description=request.description,
                facts=self.accounting_request(request),
                business_date=min(component.recognition_date for component in request.components),
                posting_date=request.posting_date,
                rule_version=RULE_VERSION,
                rule_trace=[{"stage": "component_preview", "version": RULE_VERSION}],
            )
            self.session.add(event)
            self.session.flush()
            self.common._attach_evidence(event, evidence_ids)
            self.event = event
            self.evidence_ids = set(evidence_ids)
            self._compile_plans(organization)
            reviewed = request.model_dump(mode="json")
            reviewed_components = {item["key"]: item for item in reviewed["components"]}
            components = []
            confirmation_hashes = {}
            for key, plan in self.plans.items():
                if key.startswith("funds."):
                    continue
                calculation_hash = plan.derived.get("calculation_hash")
                if plan.kind == "tax_relief":
                    calculation_hash = plan.derived["tax_period"]["calculation_hash"]
                if calculation_hash and "calculation_hash" in reviewed_components[key]:
                    reviewed_components[key]["calculation_hash"] = calculation_hash
                    confirmation_hashes[key] = calculation_hash
                elif calculation_hash and plan.kind in {
                    "fixed_asset_depreciation",
                    "fixed_asset_depreciation_batch",
                }:
                    reviewed_components[key]["facts"]["calculation_hash"] = calculation_hash
                    confirmation_hashes[key] = calculation_hash
                components.append(
                    {
                        "key": key,
                        "kind": plan.kind,
                        "calculation_hash": calculation_hash,
                        "derived": plan.derived,
                        "entries": [
                            {
                                "account_role": entry.account_role,
                                "account_code": entry.account_code,
                                "debit_fen": entry.debit_fen,
                                "credit_fen": entry.credit_fen,
                            }
                            for entry in plan.entries
                        ],
                    }
                )
            return FinanceResult(
                status=ResultStatus.CALCULATED,
                rule_version=RULE_VERSION,
                trace=[{"stage": "component_preview", "version": RULE_VERSION}],
                data={
                    "components": components,
                    "confirmation_hashes": confirmation_hashes,
                    "reviewed_request": reviewed,
                    "facts_hash": payload_hash(self.accounting_request(request)),
                },
            )
        except AccountingFactError as exc:
            return FinanceResult(**exc.result())
        except MissingFacts as exc:
            return FinanceResult(
                status=ResultStatus.NEEDS_INFORMATION,
                missing_information=exc.paths,
            )
        except (ValueError, LookupError) as exc:
            return FinanceResult(status=ResultStatus.REJECTED, errors=[str(exc)])
        except IntegrityError:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["COMPONENT_DATABASE_CONSTRAINT_VIOLATION"],
            )
        finally:
            if transaction.is_active:
                transaction.rollback()
            self.previewing = False

    def _compile_plans(self, organization: Organization) -> None:
        borrowing_sources = set()
        for component in self.request.components:
            obligation_sources = set()
            allocations = [
                (str(a.open_item_id), a.source_component_key, a.source_open_item_key)
                for a in getattr(component, "allocations", [])
            ]
            if getattr(component, "source_open_item_id", None):
                allocations.append((str(component.source_open_item_id), None, "primary"))
            if getattr(component, "source_component_key", None):
                allocations.append((str(None), component.source_component_key, "withholding_tax"))
            for allocation_source in allocations:
                if allocation_source in obligation_sources:
                    raise ValueError("DUPLICATE_OBLIGATION_SETTLEMENT_SOURCE")
                obligation_sources.add(allocation_source)
            if component.kind in {
                "borrowing_interest_payment",
                "borrowing_principal_repayment",
            }:
                source_key = (
                    component.kind,
                    component.borrowing_id,
                    component.accrual_event_id,
                    component.accrual_component_key,
                )
                if source_key in borrowing_sources:
                    raise ValueError("DUPLICATE_BORROWING_SETTLEMENT_SOURCE")
                borrowing_sources.add(source_key)
        pending = list(self.request.components)
        while pending:
            progress = False
            for component in pending[:]:
                if component.kind == "tax_relief" and any(
                    other.contributes_tax_sources for other in pending if other is not component
                ):
                    continue
                deps = self.request.component_dependencies(component)
                if deps - self.plans.keys():
                    continue
                plan = self.select_accounts(component, self.compile(component))
                plan.facts.pop("metadata", None)
                self.plans[component.key] = plan
                pending.remove(component)
                progress = True
            if not progress:
                raise ValueError("COMPONENT_DEPENDENCY_CYCLE")
        self.validate_salary_settlement_plan()
        self.validate_refund_receipts()
        self.compile_funds(organization)
        self.validate_tax_settlement_plan()

    def result(self, event: BusinessEvent) -> FinanceResult:
        voucher = self.session.scalar(select(Voucher).where(Voucher.event_id == event.id))
        items = self.session.scalars(
            select(OpenItem).where(
                OpenItem.source_event_id == event.id,
            )
        ).all()
        components = self.session.scalars(
            select(BusinessEventComponent)
            .where(
                BusinessEventComponent.event_id == event.id,
            )
            .order_by(BusinessEventComponent.ordinal)
        ).all()
        return FinanceResult(
            status=ResultStatus.POSTED
            if event.status == "reversed"
            else ResultStatus(event.status),
            event_id=event.id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            trace=event.rule_trace,
            data={
                "original_status": event.status,
                "components": [
                    {
                        "id": str(c.id),
                        "key": c.key,
                        "kind": c.kind,
                        "facts_hash": canonical_sha256(c.facts),
                        "derived": c.derived,
                        "recognition": recognition_projection(c.facts),
                        "business_reference": {
                            "event_key": event.idempotency_key,
                            "component_key": c.key,
                        },
                        "management": metadata_projection(
                            self.session, event.org_id, event.id, c.key
                        ),
                    }
                    for c in components
                ],
                "created_open_items": [
                    self.common._open_item_result(i)
                    | {
                        "source_component_id": str(i.source_component_id),
                        "component_key": i.component_key,
                    }
                    for i in items
                ],
                "facts_hash": payload_hash(event.facts),
            },
        )

    def need(self, component: Any, *fields: str) -> None:
        missing = [
            f"components.{component.key}.{f}"
            for f in fields
            if getattr(component, f, None) is None
            and not (f == "payment_date" and self.fund_dates(component))
        ]
        if missing:
            raise MissingFacts(missing)

    def fund_dates(self, component) -> set[date]:
        return {
            f.payment_date
            for f in self.request.funds
            if any(a.component_key == component.key for a in f.allocations)
        }

    def first_payment_date(self, component) -> date:
        dates = self.fund_dates(component)
        if dates:
            return min(dates)
        self.need(component, "payment_date")
        return component.payment_date

    def party(self, reference: Any) -> Counterparty | None:
        return self.common._resolve_counterparty_reference(self.request.org_id, reference)

    def plan(
        self,
        c: Any,
        entries: list[Entry],
        *,
        derived: dict | None = None,
        open_items: list[OpenItemPlan] | None = None,
        settlements: list[SettlementPlan] | None = None,
        effects: list | None = None,
        cash_line: int | None = None,
    ) -> ComponentPostingPlan:
        values = derived or {}
        if cash_line is not None:
            values["cash_flow_category"] = f"cash_flow_{cash_line}"
        plan = ComponentPostingPlan(
            key=c.key,
            kind=c.kind,
            facts=c.accounting_facts(),
            entries=entries,
            derived=values,
            rule_version=values.get("tax_rule_version", RULE_VERSION),
            open_items=open_items or [],
            settlements=settlements or [],
            effects=effects or [],
        )
        invoices = getattr(c, "invoice_references", [])
        if invoices:
            direction = "input" if c.kind == "expense" else "output"
            if sum(i.gross_amount_fen for i in invoices) > c.amount_fen:
                raise ValueError("COMPONENT_INVOICE_AMOUNT_EXCEEDED")
            if any(i.direction != direction for i in invoices):
                raise ValueError("COMPONENT_INVOICE_DIRECTION_MISMATCH")

            def invoice_effect(session, event, component):
                for reference in invoices:
                    session.add(
                        Invoice(org_id=event.org_id, event_id=event.id, **reference.model_dump())
                    )

            plan.effects.append(invoice_effect)
        return plan

    def compile(self, c: Any) -> ComponentPostingPlan:
        if hasattr(c, "amount_fen") and c.kind != "pass_through":
            self.need(c, "amount_fen")
        compiler = getattr(self, f"compile_{c.kind}", None)
        if compiler is None:
            from .domain_action_schemas import DOMAIN_REQUEST_TYPES, DomainFactsRequired
            from .domain_components import compile_domain_action

            if c.kind not in DOMAIN_REQUEST_TYPES:
                raise ValueError(f"UNSUPPORTED_BUSINESS_COMPONENT:{c.kind}")
            try:
                plan = compile_domain_action(
                    self.session,
                    kind=c.kind,
                    facts=c.facts,
                    org_id=self.request.org_id,
                    key=c.key,
                    posting_date=self.request.posting_date,
                    business_date=c.business_date,
                    payment_date=c.payment_date,
                    evidence_references=list(self.evidence_ids),
                    description="",
                )
                return plan
            except DomainFactsRequired as exc:
                raise MissingFacts([f"components.{c.key}.facts.{f}" for f in exc.fields]) from exc
        return compiler(c)

    def _compile_local_fixed_asset_depreciation(self, c, *, batch: bool):
        from .domain_action_schemas import DomainFactsRequired
        from .domain_components import compile_domain_action

        try:
            if batch:
                activation_plans = {
                    source_key: self.plans.get(source_key)
                    for source_key in c.activation_component_keys
                }
                plan = compile_domain_action(
                    self.session,
                    kind=c.kind,
                    facts=c.facts,
                    org_id=self.request.org_id,
                    key=c.key,
                    posting_date=self.request.posting_date,
                    business_date=c.business_date,
                    payment_date=c.payment_date,
                    evidence_references=list(self.evidence_ids),
                    description="",
                    activation_plans=activation_plans,
                    require_confirmation=not self.previewing,
                )
                return replace(
                    plan,
                    facts={
                        **plan.facts,
                        "activation_component_keys": list(c.activation_component_keys),
                    },
                )
            plan = compile_domain_action(
                self.session,
                kind=c.kind,
                facts=c.facts,
                org_id=self.request.org_id,
                key=c.key,
                posting_date=self.request.posting_date,
                business_date=c.business_date,
                payment_date=c.payment_date,
                evidence_references=list(self.evidence_ids),
                description="",
                activation_component_key=c.activation_component_key,
                activation_plan=self.plans.get(c.activation_component_key),
                require_confirmation=not self.previewing,
            )
            return replace(
                plan,
                facts={
                    **plan.facts,
                    "activation_component_key": c.activation_component_key,
                },
            )
        except DomainFactsRequired as exc:
            raise MissingFacts([f"components.{c.key}.facts.{f}" for f in exc.fields]) from exc

    def compile_fixed_asset_depreciation(self, c) -> ComponentPostingPlan:
        return self._compile_local_fixed_asset_depreciation(c, batch=False)

    def compile_fixed_asset_depreciation_batch(self, c) -> ComponentPostingPlan:
        return self._compile_local_fixed_asset_depreciation(c, batch=True)

    def select_accounts(self, c: Any, plan: ComponentPostingPlan) -> ComponentPostingPlan:
        """Resolve configured detail accounts within the rules already chosen by facts."""
        source = getattr(c, "source", None)
        if source is not None:
            if source.component_key:
                origin = self.plans[source.component_key]
                origin_accounts = [
                    get_account_by_role(self.session, self.request.org_id, e.account_role)
                    if e.account_role
                    else get_account_by_code(self.session, self.request.org_id, e.account_code)
                    for e in origin.entries
                ]
                origin_kind = origin.kind
            else:
                from .models import VoucherLine

                origin = self.session.get(BusinessEventComponent, source.component_id)
                origin_kind = origin.kind
                origin_accounts = self.session.scalars(
                    select(Account)
                    .join(VoucherLine, VoucherLine.account_id == Account.id)
                    .where(VoucherLine.component_id == source.component_id)
                ).all()
            inherited_classes = (
                {"contract_liability", "vat_payable"}
                if origin_kind == "customer_advance"
                else {"service_revenue", "vat_payable"}
                if origin_kind == "service_sale"
                else EXPENSE_CLASSES
                if origin_kind == "expense"
                else set()
            )
            inherited = {}
            for account in origin_accounts:
                role = account_business_class(account)
                if role in inherited_classes:
                    inherited.setdefault(role, set()).add(account.code)
            entries = []
            for entry in plan.entries:
                role = entry.account_role or account_business_class(
                    get_account_by_code(self.session, self.request.org_id, entry.account_code)
                )
                if role in inherited:
                    if len(inherited[role]) != 1:
                        raise ValueError("SOURCE_COMPONENT_ACCOUNT_AMBIGUOUS")
                    code = next(iter(inherited[role]))
                    if role in c.account_selections and c.account_selections[role] != code:
                        raise ValueError("COMPONENT_MUST_USE_SOURCE_ACCOUNT")
                    entry = replace(entry, account_role=None, account_code=code)
                entries.append(entry)
            plan = replace(plan, entries=entries)
        if not c.account_selections:
            return plan
        source_accounts = set()
        source_accounts.update(use["account_code"] for use in plan.derived.get("cost_uses", []))
        for settlement in plan.settlements:
            item, _, derived, item_key = self.obligation(settlement)
            role, code = self.obligation_account(item, derived, item_key)
            source_accounts.add(
                code or get_account_by_role(self.session, self.request.org_id, role).code
            )
        from .coa import get_account_for_business_class

        selected = {
            role: get_account_for_business_class(
                self.session,
                self.request.org_id,
                role,
                account_code=code,
            )
            for role, code in c.account_selections.items()
        }
        used = set()
        entries = []
        for entry in plan.entries:
            role = entry.account_role
            if role is None:
                role = account_business_class(
                    get_account_by_code(
                        self.session,
                        self.request.org_id,
                        entry.account_code,
                    )
                )
            if role in selected:
                if getattr(c, "account_code", None) and c.account_code != selected[role].code:
                    raise ValueError("COMPONENT_ACCOUNT_SELECTION_CONFLICT")
                source_code = (
                    entry.account_code
                    or get_account_by_role(
                        self.session, self.request.org_id, entry.account_role
                    ).code
                )
                if source_code in source_accounts and source_code != selected[role].code:
                    raise ValueError("SETTLEMENT_MUST_USE_SOURCE_ACCOUNTS")
                used.add(role)
                entry = replace(entry, account_role=None, account_code=selected[role].code)
            entries.append(entry)
        if used != selected.keys():
            raise ValueError("COMPONENT_ACCOUNT_SELECTION_NOT_USED_BY_RULE")
        items = []
        for item in plan.open_items:
            role = item.account_role or plan.derived.get("open_item_accounts", {}).get(item.key)
            items.append(
                replace(item, account_role=None, account_code=selected[role].code)
                if role in selected
                else item
            )

        def select_effect_accounts(session, event, component):
            # Domain effects may create normalized obligations directly. Their
            # origin follows the same configured role selection as their ledger leg.
            for item in session.scalars(
                select(OpenItem).where(
                    OpenItem.source_component_id == component.id,
                )
            ):
                if item.account_id:
                    role = account_business_class(session.get(Account, item.account_id))
                    if role in selected:
                        item.account_id = selected[role].id

        return replace(
            plan, entries=entries, open_items=items, effects=[*plan.effects, select_effect_accounts]
        )

    def compile_expense(self, c) -> ComponentPostingPlan:
        self.need(c, "expense_class", "payment_basis")
        if c.expense_class not in EXPENSE_CLASSES:
            raise ValueError("EXPENSE_BUSINESS_CLASS_UNSUPPORTED")
        if c.expense_nature == "bank_service_fee" and c.expense_class != "finance_expense":
            raise ValueError("BANK_SERVICE_FEE_REQUIRES_FINANCE_EXPENSE")
        from .coa import get_account_for_business_class

        account = get_account_for_business_class(
            self.session,
            self.request.org_id,
            c.expense_class,
            account_code=c.account_code,
        )
        if c.payer is not None and c.payment_basis != "person_advance":
            raise ValueError("PAYER_REQUIRES_PERSON_ADVANCE")
        party = self.party(c.payer)
        entries = [
            Entry(
                account_code=account.code,
                debit_fen=c.amount_fen,
                counterparty_id=party.id if party else None,
                memo="",
            )
        ]
        items = []
        derived = {
            "expense_class": c.expense_class,
            "expense_account_code": account.code,
            "amount_fen": c.amount_fen,
        }
        if c.payment_basis != "immediate":
            role = "accounts_payable"
            if c.payment_basis == "person_advance":
                self.need(c, "payer")
                if party.kind not in {"employee", "owner"}:
                    raise ValueError("PERSON_ADVANCE_REQUIRES_EMPLOYEE_OR_OWNER")
                role = "employee_payable" if party.kind == "employee" else "owner_payable"
            entries.append(
                Entry(
                    account_role=role,
                    credit_fen=c.amount_fen,
                    counterparty_id=party.id if party else None,
                )
            )
            items.append(
                OpenItemPlan(
                    counterparty_id=party.id if party else None,
                    item_type="payable",
                    original_amount_fen=c.amount_fen,
                )
            )
            derived["open_item_accounts"] = {"primary": role}
        return self.plan(
            c,
            entries,
            derived=derived,
            open_items=items,
            cash_line=3 if c.expense_class in {"service_cost", "labor_service_cost"} else 6,
        )

    def compile_supplier_advance(self, c):
        from .purchase_components import compile_supplier_advance

        return compile_supplier_advance(self, c)

    def compile_supplier_advance_application(self, c):
        from .purchase_components import compile_supplier_advance_application

        return compile_supplier_advance_application(self, c)

    def compile_supplier_advance_refund(self, c):
        from .purchase_components import compile_supplier_advance_refund

        return compile_supplier_advance_refund(self, c)

    def compile_project_cost(self, c):
        from .purchase_components import compile_project_cost

        return compile_project_cost(self, c)

    def compile_project_cost_expense(self, c):
        from .purchase_components import compile_project_cost_expense

        return compile_project_cost_expense(self, c)

    def compile_intangible_asset_acquisition(self, c):
        from .purchase_components import compile_intangible_asset_acquisition

        return compile_intangible_asset_acquisition(self, c)

    def sales_split(self, c, *, allow_deferred: bool = False) -> tuple[int, int, dict]:
        self.need(c, "tax_facts")
        tax = c.tax_facts
        missing = [
            f"components.{c.key}.tax_facts.{name}"
            for name in ("taxable", "invoice_type", "waive_exemption", "tax_due_on_event")
            if getattr(tax, name) is None
        ]
        if tax.taxable and tax.rate_percent is None:
            missing.append(f"components.{c.key}.tax_facts.rate_percent")
        taxable = bool(tax.taxable and (tax.tax_due_on_event or allow_deferred))
        if taxable and c.tax_obligation_date is None:
            missing.append(f"components.{c.key}.tax_obligation_date")
        if missing:
            raise MissingFacts(missing)
        if taxable:
            if self.common._tax_obligation_date_is_locked(
                self.request.org_id,
                c.tax_obligation_date,
            ):
                raise ValueError("TAX_PERIOD_SOURCE_LOCKED")
        net, vat = (
            split_tax_inclusive(c.amount_fen, tax.rate_percent) if taxable else (c.amount_fen, 0)
        )
        deferred = bool(
            allow_deferred and taxable and c.tax_obligation_date > self.request.posting_date
        )
        rule = active_tax_rule(
            self.session,
            self.session.get(Organization, self.request.org_id),
            c.tax_obligation_date or self.request.posting_date,
        )
        return (
            net,
            vat,
            {
                "amount_fen": c.amount_fen,
                "taxable_gross_fen": c.amount_fen if taxable else 0,
                "net_sales_fen": net if taxable else 0,
                "vat_fen": vat,
                "exemption_eligible": bool(
                    taxable and tax.invoice_type != "special" and not tax.waive_exemption
                ),
                "tax_due_on_event": tax.tax_due_on_event,
                "tax_obligation_date": c.tax_obligation_date.isoformat()
                if c.tax_obligation_date
                else None,
                "vat_recognition": "deferred" if deferred else "payable",
                "tax_rule_version": rule.version,
                "tax_rule_source_url": rule.source_url,
                "tax_rule_id": str(rule.id),
            },
        )

    def compile_service_sale(self, c) -> ComponentPostingPlan:
        self.need(c, "recognition_basis", "fulfillment_date")
        if c.fulfillment_date > self.request.posting_date:
            raise ValueError("SERVICE_NOT_FULFILLED_AT_POSTING")
        net, vat, derived = self.sales_split(c, allow_deferred=c.recognition_basis == "credit")
        entries = [Entry(account_role="service_revenue", credit_fen=net, counterparty_id=None)]
        if vat:
            entries.append(
                Entry(
                    account_role="deferred_output_vat"
                    if derived["vat_recognition"] == "deferred"
                    else "vat_payable",
                    credit_fen=vat,
                )
            )
        items = []
        if c.recognition_basis == "credit":
            entries.insert(
                0,
                Entry(
                    account_role="accounts_receivable",
                    debit_fen=c.amount_fen,
                    counterparty_id=None,
                ),
            )
            items.append(
                OpenItemPlan(
                    counterparty_id=None,
                    item_type="receivable",
                    original_amount_fen=c.amount_fen,
                )
            )
            derived["open_item_accounts"] = {"primary": "accounts_receivable"}
        return self.plan(c, entries, derived=derived, open_items=items, cash_line=1)

    def compile_customer_advance(self, c) -> ComponentPostingPlan:
        self.need(c, "tax_facts")
        if c.tax_facts.tax_due_on_event is None:
            raise MissingFacts([f"components.{c.key}.tax_facts.tax_due_on_event"])
        if c.tax_facts.tax_due_on_event:
            net, vat, derived = self.sales_split(c)
        else:
            net, vat, derived = c.amount_fen, 0, {"amount_fen": c.amount_fen}
        derived.update(
            {
                "advance_fen": c.amount_fen,
                "liability_fen": net,
                "tax_previously_accrued": c.tax_facts.tax_due_on_event,
            }
        )
        entries = [Entry(account_role="contract_liability", credit_fen=net, counterparty_id=None)]
        if vat:
            entries.append(Entry(account_role="vat_payable", credit_fen=vat))
        return self.plan(c, entries, derived=derived, cash_line=1)

    def source(self, c, allowed_kinds: set[str]) -> tuple[dict, dict, uuid.UUID | None]:
        ref = c.source
        if ref.component_key:
            plan = self.plans[ref.component_key]
            if plan.kind not in allowed_kinds:
                raise ValueError("SOURCE_COMPONENT_KIND_MISMATCH")
            return plan.facts, plan.derived, None
        source = self.session.scalar(
            select(BusinessEventComponent)
            .where(
                BusinessEventComponent.id == ref.component_id,
                BusinessEventComponent.org_id == self.request.org_id,
            )
            .with_for_update()
        )
        if source is None or source.kind not in allowed_kinds:
            raise ValueError("SOURCE_COMPONENT_NOT_FOUND_OR_KIND_MISMATCH")
        event = self.session.get(BusinessEvent, source.event_id)
        if event.status != "posted" or event.posting_date > self.request.posting_date:
            raise ValueError("SOURCE_COMPONENT_NOT_ACTIVE_OR_FUTURE")
        return source.facts, source.derived, source.event_id

    def source_usage(self, c, original_amount: int) -> int:
        """Count local and persisted uses by the identity of the source component."""
        ref = c.source.model_dump(mode="json", exclude_none=True)
        total = sum(
            p.derived.get("source_used_fen", 0)
            for p in self.plans.values()
            if p.key != c.key
            and {k: v for k, v in p.facts.get("source", {}).items() if v is not None} == ref
        )
        if c.source.component_id:
            source = self.session.get(BusinessEventComponent, c.source.component_id)
            rows = self.session.scalars(
                select(BusinessEventComponent)
                .join(
                    BusinessEvent,
                    BusinessEvent.id == BusinessEventComponent.event_id,
                )
                .where(
                    BusinessEventComponent.org_id == self.request.org_id,
                    BusinessEvent.status == "posted",
                )
            )
            for row in rows:
                reference = row.facts.get("source") or {}
                same_source = reference.get("component_id") == str(source.id) or (
                    row.event_id == source.event_id and reference.get("component_key") == source.key
                )
                if same_source:
                    total += row.derived.get("source_used_fen", 0)
        if total + c.amount_fen > original_amount:
            raise ValueError("COMPONENT_SOURCE_AMOUNT_EXCEEDED")
        return total

    def validate_refund_receipts(self) -> None:
        from .component_refunds import validate_refund_receipts

        validate_refund_receipts(self)

    @staticmethod
    def source_tax_split(facts: dict, amount_fen: int, used_fen: int) -> tuple[int, int]:
        """Allocate source rounding cumulatively so its final cent is consumed once."""
        from decimal import Decimal

        tax = facts["tax_facts"]
        if not tax["taxable"]:
            return amount_fen, 0
        rate = Decimal(tax["rate_percent"])
        previous_net = split_tax_inclusive(used_fen, rate)[0] if used_fen else 0
        cumulative_net, _ = split_tax_inclusive(used_fen + amount_fen, rate)
        net = cumulative_net - previous_net
        return net, amount_fen - net

    def source_dependency(
        self, source_event_id: uuid.UUID | None, source_component_id=None, amount_fen=0
    ):
        if source_event_id is not None and source_component_id is None:
            raise ValueError("SOURCE_COMPONENT_REFERENCE_REQUIRED")

        def effect(session, event, component):
            if source_event_id is not None:
                parent_component_id = source_component_id
                existing = session.scalar(
                    select(BusinessEventDependency.id).where(
                        BusinessEventDependency.parent_event_id == source_event_id,
                        BusinessEventDependency.child_event_id == event.id,
                        BusinessEventDependency.parent_component_id == parent_component_id,
                        BusinessEventDependency.child_component_id == component.id,
                        BusinessEventDependency.dependency_kind == "component_source",
                    )
                )
                if existing is None:
                    session.add(
                        BusinessEventDependency(
                            org_id=event.org_id,
                            parent_event_id=source_event_id,
                            child_event_id=event.id,
                            dependency_kind="component_source",
                            parent_component_id=parent_component_id,
                            child_component_id=component.id,
                            amount_fen=amount_fen,
                        )
                    )

        return effect

    def compile_service_fulfillment(self, c) -> ComponentPostingPlan:
        self.need(c, "fulfillment_date")
        if c.fulfillment_date > self.request.posting_date:
            raise ValueError("SERVICE_NOT_FULFILLED_AT_POSTING")
        facts, source, source_event = self.source(c, {"customer_advance"})
        used_fen = self.source_usage(c, source["advance_fen"])
        if source["tax_previously_accrued"]:
            net, _ = self.source_tax_split(facts, c.amount_fen, used_fen)
            vat, derived = 0, {}
        else:
            net, vat, derived = self.sales_split(c)
        entries = [
            Entry(account_role="contract_liability", debit_fen=net + vat, counterparty_id=None),
            Entry(account_role="service_revenue", credit_fen=net, counterparty_id=None),
        ]
        if vat:
            entries.append(Entry(account_role="vat_payable", credit_fen=vat))
        derived["source_used_fen"] = c.amount_fen
        derived["source_usage_before_fen"] = used_fen
        return self.plan(
            c,
            entries,
            derived=derived,
            effects=[self.source_dependency(source_event, c.source.component_id, c.amount_fen)],
        )

    def compile_customer_refund(self, c) -> ComponentPostingPlan:
        self.need(c, "payment_date")
        facts, source, source_event = self.source(
            c,
            {"customer_advance"} if c.refund_kind == "advance" else {"service_sale"},
        )
        used_fen = self.source_usage(c, source["amount_fen"])
        if c.refund_kind == "sale_return" or source.get("tax_previously_accrued"):
            self.need(c, "tax_facts")
            original_tax = facts.get("tax_facts")
            refund_tax = c.tax_facts.model_dump(mode="json")
            if original_tax is None or any(
                original_tax.get(field) != refund_tax.get(field)
                for field in ("taxable", "rate_percent", "invoice_type", "waive_exemption")
            ):
                raise ValueError("REFUND_MUST_USE_SOURCE_TAX_FACTS")
        if c.refund_kind == "advance":
            if source.get("tax_previously_accrued"):
                net, vat, derived = self.sales_split(c)
            else:
                net, vat, derived = c.amount_fen, 0, {}
            role = "contract_liability"
        else:
            net, vat, derived = self.sales_split(c)
            role = "service_revenue"
        if source.get("vat_fen") and not derived.get("taxable_gross_fen"):
            raise ValueError("REFUND_VAT_RECOGNITION_FACTS_INCONSISTENT")
        if derived.get("taxable_gross_fen"):
            net, vat = self.source_tax_split(facts, c.amount_fen, used_fen)
            derived.update(net_sales_fen=net, vat_fen=vat)
        derived = {
            k: -v if k in {"taxable_gross_fen", "net_sales_fen", "vat_fen"} else v
            for k, v in derived.items()
        }
        entries = [Entry(account_role=role, debit_fen=net, counterparty_id=None)]
        if vat:
            entries.append(Entry(account_role="vat_payable", debit_fen=vat))
        derived["source_used_fen"] = c.amount_fen
        derived["source_usage_before_fen"] = used_fen
        return self.plan(
            c,
            entries,
            derived=derived,
            effects=[self.source_dependency(source_event, c.source.component_id, c.amount_fen)],
            cash_line=1,
        )

    def obligation(self, allocation) -> tuple[Any, dict, dict, str]:
        if allocation.source_component_key:
            plan = self.plans[allocation.source_component_key]
            item = next(
                (i for i in plan.open_items if i.key == allocation.source_open_item_key), None
            )
            if item is None:
                raise ValueError("COMPONENT_OPEN_ITEM_NOT_FOUND")
            return item, plan.facts, plan.derived, allocation.source_open_item_key
        item = self.session.scalar(
            select(OpenItem)
            .where(
                OpenItem.org_id == self.request.org_id,
                OpenItem.id == allocation.open_item_id,
            )
            .with_for_update()
        )
        if item is None:
            raise ValueError("OPEN_ITEM_NOT_FOUND")
        if item.source_component_id is None:
            raise ValueError("OPEN_ITEM_COMPONENT_REQUIRED")
        source = self.session.get(BusinessEventComponent, item.source_component_id)
        return item, source.facts, source.derived, item.component_key

    def obligation_account(self, item, derived, key) -> tuple[str | None, str | None]:
        if getattr(item, "account_code", None):
            return None, item.account_code
        if getattr(item, "account_id", None):
            account = self.session.get(Account, item.account_id)
            return None, account.code
        role = getattr(item, "account_role", None) or derived.get("open_item_accounts", {}).get(key)
        if role is None:
            role = PAYABLE_ROLES.get(item.payable_category)
        if role is None:
            raise ValueError("OPEN_ITEM_ACCOUNT_ORIGIN_REQUIRED")
        return role, None

    def settlement_plans(self, c, *, transfer: bool = False):
        entries, settlements, cash_parts = [], [], []
        expected = "receivable" if c.kind == "receivable_settlement" else "payable"
        for a in c.allocations:
            item, facts, derived, key = self.obligation(a)
            if facts.get("kind") == "supplier_advance":
                raise ValueError("SUPPLIER_ADVANCE_REQUIRES_TYPED_APPLICATION_OR_REFUND")
            if item.item_type != expected:
                raise ValueError("OPEN_ITEM_DIRECTION_MISMATCH")
            if item.payable_category in {
                "salary",
                "labor_remuneration",
                "labor_individual_income_tax",
            }:
                raise ValueError("OBLIGATION_REQUIRES_WITHHOLDING_COMPONENT")
            role, code = self.obligation_account(item, derived, key)
            entries.append(
                Entry(
                    account_role=role,
                    account_code=code,
                    counterparty_id=item.counterparty_id,
                    debit_fen=a.amount_fen if expected == "payable" else 0,
                    credit_fen=a.amount_fen if expected == "receivable" else 0,
                )
            )
            settlements.append(
                SettlementPlan(
                    amount_fen=a.amount_fen,
                    purpose="debt_transfer" if transfer else c.kind,
                    expected_item_type=expected,
                    counterparty_id=item.counterparty_id,
                    open_item_id=a.open_item_id,
                    source_component_key=a.source_component_key,
                    source_open_item_key=a.source_open_item_key,
                )
            )
            account = (
                get_account_by_role(self.session, self.request.org_id, role)
                if role
                else get_account_by_code(self.session, self.request.org_id, code)
            )
            category = derived.get("open_item_cash_flow_categories", {}).get(key) or derived.get(
                "cash_flow_category"
            )
            account_class = account_business_class(account)
            if item.payable_category == "individual_income_tax":
                category = "cash_flow_5"
            elif item.payable_category in PAYABLE_ROLES and item.payable_category != "pass_through":
                category = "cash_flow_4"
            elif account_class == "pass_through_payable":
                category = "cash_flow_6"
            elif facts.get("kind") == "owner_funding" and facts.get("funding_kind") == "loan":
                category = "cash_flow_16"
            elif account_class == "employee_receivable":
                category = "cash_flow_2"
            if category is None:
                raise ValueError("OBLIGATION_CASH_FLOW_ORIGIN_REQUIRED")
            cash_parts.append(
                {
                    "category": category,
                    "amount_fen": a.amount_fen,
                    "source": a.model_dump(mode="json", exclude={"amount_fen"}),
                }
            )
        return entries, settlements, cash_parts

    def compile_receivable_settlement(self, c) -> ComponentPostingPlan:
        self.need(c, "payment_date")
        entries, settlements, parts = self.settlement_plans(c)
        effects = []
        for a in c.allocations:
            item, facts, derived, _ = self.obligation(a)
            if derived.get("vat_recognition") == "deferred":
                due_date = date.fromisoformat(derived["tax_obligation_date"])
                if self.first_payment_date(c) < due_date:
                    raise ValueError("DEFERRED_VAT_EARLY_RECEIPT_REQUIRES_FACT_CORRECTION")
                if self.request.posting_date != due_date:
                    raise ValueError(
                        "DEFERRED_OUTPUT_VAT_TRANSFER_POSTING_DATE_MUST_EQUAL_TAX_OBLIGATION_DATE"
                    )
                if a.open_item_id is None:
                    raise ValueError("DEFERRED_VAT_REQUIRES_POSTED_SOURCE")
                transferred = self.session.scalar(
                    select(DeferredOutputVatTransfer.id)
                    .join(
                        BusinessEvent,
                        BusinessEvent.id == DeferredOutputVatTransfer.transfer_event_id,
                    )
                    .where(
                        DeferredOutputVatTransfer.source_open_item_id == item.id,
                        BusinessEvent.status == "posted",
                    )
                )
                planned = any(
                    str(item.id) in p.derived.get("deferred_vat_source_items", [])
                    for p in self.plans.values()
                )
                if transferred is None and not planned:
                    vat = derived["vat_fen"]
                    transfer_entries = deferred_vat_transfer_entries(
                        self.session,
                        self.request.org_id,
                        item.source_component_id,
                        vat,
                        payable_account_code=c.account_selections.get("vat_payable"),
                    )
                    selected_deferred = c.account_selections.get("deferred_output_vat")
                    if selected_deferred and selected_deferred != transfer_entries[0].account_code:
                        raise ValueError("COMPONENT_MUST_USE_SOURCE_ACCOUNT")
                    entries += transfer_entries

                    def transfer(session, event, component, item=item, vat=vat, due=due_date):
                        session.add(
                            DeferredOutputVatTransfer(
                                org_id=event.org_id,
                                source_event_id=item.source_event_id,
                                source_open_item_id=item.id,
                                transfer_event_id=event.id,
                                amount_fen=vat,
                                tax_obligation_date=due,
                                accounting_rule_version=self.common.DEFERRED_OUTPUT_VAT_RULE_VERSION,
                                accounting_rule_source_url=self.common.DEFERRED_OUTPUT_VAT_RULE_SOURCE_URL,
                            )
                        )

                    effects.append(transfer)
        return self.plan(
            c,
            entries,
            settlements=settlements,
            effects=effects,
            derived={
                "cash_flow_parts": parts,
                "deferred_vat_source_items": [str(a.open_item_id) for a in c.allocations],
            },
        )

    def compile_payable_settlement(self, c) -> ComponentPostingPlan:
        self.need(c, "payment_date")
        entries, settlements, parts = self.settlement_plans(c)
        from .models import PayrollEventLink

        effects = []
        for allocation in c.allocations:
            item, source_facts, source_derived, _ = self.obligation(allocation)
            if (
                item.payable_category not in PAYABLE_ROLES
                or item.payable_category == "pass_through"
            ):
                continue
            if allocation.source_component_key:
                batch_ids = {
                    uuid.UUID(value) for value in source_derived.get("payroll_batch_ids", [])
                }
                if source_derived.get("payroll_batch_id"):
                    batch_ids.add(uuid.UUID(source_derived["payroll_batch_id"]))
            else:
                batch_ids = set(
                    self.session.scalars(
                        select(PayrollEventLink.payroll_batch_id).where(
                            PayrollEventLink.event_id == item.source_event_id,
                            PayrollEventLink.component_id == item.source_component_id,
                            PayrollEventLink.link_kind != "reversal",
                        )
                    )
                )
            if not batch_ids:
                if source_facts.get("kind") == "payroll_contribution_supplement":
                    continue
                raise ValueError("STATUTORY_OBLIGATION_PAYROLL_LINK_REQUIRED")

            def link_payroll(
                session, event, component, batch_ids=batch_ids, item=item, allocation=allocation
            ):
                source_item = item
                if allocation.source_component_key:
                    source_item = session.scalar(
                        select(OpenItem)
                        .join(
                            BusinessEventComponent,
                            BusinessEventComponent.id == OpenItem.source_component_id,
                        )
                        .where(
                            BusinessEventComponent.event_id == event.id,
                            BusinessEventComponent.key == allocation.source_component_key,
                            OpenItem.component_key == allocation.source_open_item_key,
                        )
                    )
                    if source_item is None:
                        raise ValueError("STATUTORY_COMPONENT_OPEN_ITEM_NOT_FOUND")
                for batch_id in batch_ids:
                    session.add(
                        PayrollEventLink(
                            org_id=event.org_id,
                            event_id=event.id,
                            component_id=component.id,
                            payroll_batch_id=batch_id,
                            source_payment_event_id=source_item.source_event_id,
                            source_open_item_id=source_item.id,
                            link_kind="statutory_payment",
                        )
                    )

            effects.append(link_payroll)
        return self.plan(
            c, entries, settlements=settlements, effects=effects, derived={"cash_flow_parts": parts}
        )

    def compile_pass_through(self, c) -> ComponentPostingPlan:
        if c.amount_fen is None:
            raise AccountingFactError(
                AccountingFactIssue(
                    code="PASS_THROUGH_AMOUNT_REQUIRED",
                    kind="missing_accounting_fact",
                    fields=[f"components.{c.key}.amount_fen"],
                    expected={"unit": "fen", "type": "integer"},
                    message="请从原资料或负责人确认中核定代收代付金额。",
                ),
                [f"components.{c.key}.amount_fen"],
            )
        if c.recognition_basis == "credit":
            return self.plan(
                c,
                [
                    Entry(account_role="pass_through_receivable", debit_fen=c.amount_fen),
                    Entry(account_role="pass_through_payable", credit_fen=c.amount_fen),
                ],
                open_items=[
                    OpenItemPlan(
                        counterparty_id=None,
                        key="receivable",
                        item_type="receivable",
                        original_amount_fen=c.amount_fen,
                    ),
                    OpenItemPlan(
                        counterparty_id=None,
                        item_type="payable",
                        original_amount_fen=c.amount_fen,
                        payable_category="pass_through",
                        pass_through_key=c.key,
                    ),
                ],
                derived={
                    "open_item_accounts": {
                        "receivable": "pass_through_receivable",
                        "primary": "pass_through_payable",
                    },
                    "open_item_cash_flow_categories": {
                        "receivable": "cash_flow_2",
                        "primary": "cash_flow_6",
                    },
                    "amount_fen": c.amount_fen,
                },
            )
        self.need(c, "payment_date")
        if c.recognition_period:
            raise ValueError("PASS_THROUGH_RECEIPT_REQUIRES_ACTUAL_FUNDS_DATE")
        return self.plan(
            c,
            [Entry(account_role="pass_through_payable", credit_fen=c.amount_fen)],
            open_items=[
                OpenItemPlan(
                    counterparty_id=None,
                    item_type="payable",
                    original_amount_fen=c.amount_fen,
                    payable_category="pass_through",
                    pass_through_key=c.key,
                )
            ],
            derived={
                "open_item_accounts": {"primary": "pass_through_payable"},
                "amount_fen": c.amount_fen,
            },
            cash_line=2,
        )

    def compile_debt_transfer(self, c) -> ComponentPostingPlan:
        self.need(c, "payer")
        payer = self.party(c.payer)
        if payer.kind not in {"owner", "employee"}:
            raise ValueError("DEBT_TRANSFER_PAYER_MUST_BE_PERSON")
        entries, settlements, parts = self.settlement_plans(c, transfer=True)
        amount = sum(a.amount_fen for a in c.allocations)
        role = "owner_payable" if payer.kind == "owner" else "employee_payable"
        entries.append(Entry(account_role=role, credit_fen=amount, counterparty_id=payer.id))
        by_category = {}
        for part in parts:
            by_category[part["category"]] = (
                by_category.get(part["category"], 0) + part["amount_fen"]
            )
        origins = {
            "primary" if len(by_category) == 1 else category: category
            for category in sorted(by_category)
        }
        return self.plan(
            c,
            entries,
            settlements=settlements,
            open_items=[
                OpenItemPlan(
                    key=key,
                    counterparty_id=payer.id,
                    item_type="payable",
                    original_amount_fen=by_category[category],
                    account_role=role,
                )
                for key, category in origins.items()
            ],
            derived={
                "open_item_accounts": {key: role for key in origins},
                "open_item_cash_flow_categories": origins,
                "transferred_sources": parts,
            },
        )

    def compile_refundable_deposit(self, c) -> ComponentPostingPlan:
        if not c.advanced_by:
            self.need(c, "payment_date")
        entries = [
            Entry(
                account_role="employee_receivable",
                debit_fen=c.amount_fen,
                counterparty_id=None,
            )
        ]
        items = [
            OpenItemPlan(
                counterparty_id=None, item_type="receivable", original_amount_fen=c.amount_fen
            )
        ]
        roles = {"primary": "employee_receivable"}
        if c.advanced_by:
            payer = self.party(c.advanced_by)
            if payer.kind not in {"employee", "owner"}:
                raise ValueError("DEPOSIT_ADVANCING_PERSON_REQUIRED")
            role = "owner_payable" if payer.kind == "owner" else "employee_payable"
            entries.append(
                Entry(account_role=role, credit_fen=c.amount_fen, counterparty_id=payer.id)
            )
            items.append(
                OpenItemPlan(
                    key="advance",
                    counterparty_id=payer.id,
                    item_type="payable",
                    original_amount_fen=c.amount_fen,
                )
            )
            roles["advance"] = role
        return self.plan(
            c, entries, open_items=items, derived={"open_item_accounts": roles}, cash_line=6
        )

    def compile_owner_funding(self, c) -> ComponentPostingPlan:
        self.need(c, "counterparty", "funding_kind", "payment_date")
        party = self.party(c.counterparty)
        if party.kind != "owner":
            raise ValueError("OWNER_FUNDING_REQUIRES_OWNER")
        role = "owner_payable" if c.funding_kind == "loan" else "paid_in_capital"
        items = (
            [
                OpenItemPlan(
                    counterparty_id=party.id, item_type="payable", original_amount_fen=c.amount_fen
                )
            ]
            if c.funding_kind == "loan"
            else []
        )
        return self.plan(
            c,
            [Entry(account_role=role, credit_fen=c.amount_fen, counterparty_id=party.id)],
            open_items=items,
            derived={"open_item_accounts": {"primary": role}},
            cash_line=14 if c.funding_kind == "loan" else 15,
        )

    def compile_other_income(self, c) -> ComponentPostingPlan:
        self.need(c, "income_kind", "payment_date")
        role = "finance_expense" if c.income_kind == "bank_interest" else "tax_relief_income"
        return self.plan(
            c,
            [
                Entry(
                    account_role=role,
                    credit_fen=c.amount_fen,
                    counterparty_id=None,
                )
            ],
            cash_line=2,
        )

    def compile_expense_recovery(self, c) -> ComponentPostingPlan:
        facts, derived, source_event = self.source(c, {"expense"})
        if facts.get("payment_basis") != "immediate":
            raise ValueError("EXPENSE_RECOVERY_REQUIRES_PAID_EXPENSE")
        self.source_usage(c, derived["amount_fen"])
        return self.plan(
            c,
            [Entry(account_code=derived["expense_account_code"], credit_fen=c.amount_fen)],
            derived={"source_used_fen": c.amount_fen},
            effects=[self.source_dependency(source_event, c.source.component_id, c.amount_fen)],
            cash_line=2,
        )

    def compile_managed_account_return(self, c) -> ComponentPostingPlan:
        self.need(c, "expense_class", "payment_date")
        return self.plan(
            c,
            [Entry(account_role=c.expense_class, credit_fen=c.amount_fen)],
            derived={
                "expense_class": c.expense_class,
                "recovery_basis": "owner_managed_payment_account_return",
            },
            cash_line=2,
        )

    def compile_expense_reserve_settlement(self, c) -> ComponentPostingPlan:
        if not c.allocations:
            raise MissingFacts([f"components.{c.key}.allocations"])
        plan = self.compile_expense_recovery(c)
        entries, settlements, _parts = self.settlement_plans(c)
        if sum(a.amount_fen for a in c.allocations) != c.amount_fen:
            raise ValueError("EXPENSE_RESERVE_SETTLEMENT_AMOUNT_MISMATCH")
        return replace(
            plan,
            entries=entries + plan.entries,
            settlements=settlements,
            derived={"source_used_fen": c.amount_fen},
        )

    def compile_funds_transfer(self, c) -> ComponentPostingPlan:
        return self.plan(
            c, [], derived={"amount_fen": c.amount_fen, "cash_flow_category": "internal_transfer"}
        )

    def compile_tax_relief(self, c) -> ComponentPostingPlan:
        from .tax_confirmation_components import compile_tax_relief

        if not self.previewing:
            self.need(c, "calculation_hash")
        return compile_tax_relief(self, c)

    def compile_enterprise_income_tax_assessment(self, c) -> ComponentPostingPlan:
        from .tax_confirmation_components import compile_enterprise_income_tax_assessment

        return compile_enterprise_income_tax_assessment(self, c)

    def compile_enterprise_income_tax_result(self, c) -> ComponentPostingPlan:
        from .tax_confirmation_components import compile_enterprise_income_tax_result

        if not self.previewing:
            self.need(c, "calculation_hash")
        return compile_enterprise_income_tax_result(self, c)

    def compile_tax_settlement(self, c) -> ComponentPostingPlan:
        self.need(c, "payment_date")
        effects = []
        entries = None
        derived = {}
        if c.tax_type != "enterprise_income_tax":
            self.need(c, "period_start", "period_end")
            if c.settlement_kind == "refund":
                raise ValueError("TAX_REFUND_REQUIRES_INCOME_TAX_RESULT")
            period = None
            if c.assessment_component_key:
                assessment = self.plans.get(c.assessment_component_key)
                if assessment is None or assessment.kind != "tax_relief":
                    raise ValueError("TAX_SETTLEMENT_ASSESSMENT_COMPONENT_INVALID")
                calculation = assessment.derived.get("tax_period")
                if not isinstance(calculation, dict):
                    raise ValueError("TAX_SETTLEMENT_ASSESSMENT_COMPONENT_INVALID")
                if (
                    assessment.facts.get("start_date") != c.period_start.isoformat()
                    or assessment.facts.get("end_date") != c.period_end.isoformat()
                ):
                    raise ValueError("TAX_SETTLEMENT_PERIOD_MISMATCH")
                period = SimpleNamespace(
                    start_date=c.period_start,
                    end_date=c.period_end,
                    adjustment_posting_date=self.request.posting_date,
                    adjustment_event_id=None,
                    adjustment_component_key=c.assessment_component_key,
                    calculation=calculation,
                )
                derived = {
                    "tax_assessment_component_key": c.assessment_component_key,
                    "tax_assessment_calculation_hash": calculation["calculation_hash"],
                }
            else:
                period = self.session.scalar(
                    select(TaxPeriod)
                    .where(
                        TaxPeriod.org_id == self.request.org_id,
                        TaxPeriod.status == "posted",
                        TaxPeriod.start_date == c.period_start,
                        TaxPeriod.end_date == c.period_end,
                    )
                    .with_for_update()
                )
            if period is None:
                if c.tax_type != "vat":
                    raise ValueError("TAX_SETTLEMENT_PERIOD_NOT_CONFIRMED")
                period = self._matching_no_adjustment_confirmation(c)
                derived = {
                    "tax_confirmation_id": str(period.id),
                    "tax_confirmation_hash": period.calculation_hash,
                }
            if (
                self.first_payment_date(c) < period.adjustment_posting_date
                or self.request.posting_date < period.adjustment_posting_date
            ):
                raise ValueError("TAX_SETTLEMENT_PRECEDES_ASSESSMENT")
            role = "vat_payable" if c.tax_type == "vat" else "surtax_payable"
            try:
                entries = tax_settlement_entries(
                    self.session,
                    self.request.org_id,
                    period,
                    c.tax_type,
                    c.amount_fen,
                    selected_account_code=c.account_selections.get(role)
                    or (
                        c.account_selections.get("deferred_output_vat")
                        if c.tax_type == "vat"
                        else None
                    ),
                    pending_plans=tuple(self.plans.values()),
                )
            except ValueError as exc:
                if str(exc) == "TAX_SETTLEMENT_ACCOUNT_SOURCE_AMBIGUOUS":
                    raise MissingFacts([f"components.{c.key}.account_selections.{role}"]) from exc
                raise
            for source in period.calculation["source_event_snapshots"]:
                effects.append(
                    self.source_dependency(
                        uuid.UUID(source["event_id"]),
                        uuid.UUID(source["component_id"]),
                        c.amount_fen,
                    )
                )
            adjustment_event_id = getattr(period, "adjustment_event_id", None)
            if adjustment_event_id is not None:
                effects.append(
                    self.source_dependency(
                        adjustment_event_id,
                        period.component_id,
                        c.amount_fen,
                    )
                )
        role = {
            "vat": "vat_payable",
            "surtax": "surtax_payable",
            "enterprise_income_tax": "enterprise_income_tax_payable",
        }[c.tax_type]
        if c.tax_type == "enterprise_income_tax":
            entries = [
                Entry(
                    account_role=role,
                    debit_fen=c.amount_fen if c.settlement_kind == "payment" else 0,
                    credit_fen=c.amount_fen if c.settlement_kind == "refund" else 0,
                )
            ]
            from .enterprise_income_tax import EnterpriseIncomeTaxService

            if not c.income_tax_allocations:
                raise MissingFacts([f"components.{c.key}.income_tax_allocations"])
            service = EnterpriseIncomeTaxService(self.session)
            effects.append(self._income_tax_effect(service, c))
        return self.plan(
            c,
            entries,
            derived=derived,
            effects=effects,
            cash_line=2 if c.settlement_kind == "refund" else 5,
        )

    def _fresh_no_adjustment_confirmation(self, confirmation: ZeroTaxPeriodConfirmation) -> bool:
        organization = self.session.get(Organization, self.request.org_id)
        if organization is None or confirmation.org_id != organization.id:
            return False
        current = calculate_tax_period(
            self.session,
            organization,
            confirmation.start_date,
            confirmation.end_date,
            confirmation.adjustment_posting_date,
        )
        return bool(
            current.vat_relief_fen == 0
            and current.surtax_total_fen == 0
            and confirmation.calculation_hash == current.calculation_hash
            and confirmation.calculation_hash_payload == current.calculation_hash_payload
            and confirmation.calculation == current.to_dict()
        )

    def _matching_no_adjustment_confirmation(self, c) -> ZeroTaxPeriodConfirmation:
        confirmations = list(
            self.session.scalars(
                select(ZeroTaxPeriodConfirmation)
                .where(
                    ZeroTaxPeriodConfirmation.org_id == self.request.org_id,
                    ZeroTaxPeriodConfirmation.start_date == c.period_start,
                    ZeroTaxPeriodConfirmation.end_date == c.period_end,
                )
                .order_by(
                    ZeroTaxPeriodConfirmation.created_at.desc(),
                    ZeroTaxPeriodConfirmation.id.desc(),
                )
                .with_for_update()
            )
        )
        if not confirmations:
            raise ValueError("TAX_SETTLEMENT_PERIOD_NOT_CONFIRMED")
        for confirmation in confirmations:
            if self._fresh_no_adjustment_confirmation(confirmation):
                return confirmation
        raise ValueError("TAX_PERIOD_CALCULATION_STALE")

    def validate_tax_settlement_plan(self) -> None:
        """Recheck confirmation lineage against the final whole-event plan."""

        for settlement in self.plans.values():
            confirmation_id = settlement.derived.get("tax_confirmation_id")
            if confirmation_id is None:
                continue
            confirmation = self.session.get(ZeroTaxPeriodConfirmation, uuid.UUID(confirmation_id))
            if confirmation is None or not self._fresh_no_adjustment_confirmation(confirmation):
                raise ValueError("TAX_PERIOD_CALCULATION_STALE")
            for source in self.plans.values():
                gross_fen = int(source.derived.get("taxable_gross_fen", 0))
                obligation_value = source.derived.get("tax_obligation_date")
                if not gross_fen or obligation_value is None:
                    continue
                obligation_date = (
                    obligation_value
                    if isinstance(obligation_value, date)
                    else date.fromisoformat(str(obligation_value))
                )
                if confirmation.start_date <= obligation_date <= confirmation.end_date:
                    raise ValueError("TAX_PERIOD_CALCULATION_STALE")

    def _income_tax_effect(self, service, c):
        # Explicit source allocation validation is shared with the CIT domain.
        def effect(session, event, component):
            from .enterprise_income_tax_schemas import IncomeTaxSourceAllocation
            from .models import (
                EnterpriseIncomeTaxQuarterConfirmation,
                EnterpriseIncomeTaxResult,
            )

            allocations = []
            for allocation in c.income_tax_allocations:
                if allocation.source_id is not None:
                    allocations.append(allocation)
                    continue
                source_component = session.scalar(
                    select(BusinessEventComponent).where(
                        BusinessEventComponent.event_id == event.id,
                        BusinessEventComponent.key == allocation.source_component_key,
                    )
                )
                if source_component is None:
                    raise ValueError("CIT_LOCAL_SOURCE_COMPONENT_NOT_FOUND")
                source_id = session.scalar(
                    select(EnterpriseIncomeTaxResult.id).where(
                        EnterpriseIncomeTaxResult.component_id == source_component.id
                    )
                ) or session.scalar(
                    select(EnterpriseIncomeTaxQuarterConfirmation.id).where(
                        EnterpriseIncomeTaxQuarterConfirmation.component_id == source_component.id
                    )
                )
                if source_id is None:
                    raise ValueError("CIT_LOCAL_SOURCE_COMPONENT_INVALID")
                allocations.append(
                    IncomeTaxSourceAllocation(
                        source_id=source_id,
                        amount_fen=allocation.amount_fen,
                    )
                )
            resolved = c.model_copy(update={"income_tax_allocations": allocations})
            service.record_component_settlement(event, component, resolved)

        return effect

    def compile_borrowing_interest_payment(self, c) -> ComponentPostingPlan:
        return self._borrowing_payment(c)

    def compile_borrowing_principal_repayment(self, c) -> ComponentPostingPlan:
        return self._borrowing_payment(c)

    def _borrowing_payment(self, c) -> ComponentPostingPlan:
        from .domain_components import compile_borrowing_payment
        from .models import BorrowingInterestAccrual

        self.need(c, "payment_date")
        if c.payment_date is None:
            # The borrowing mechanism supports full principal / accrual payoff.
            # Partial payoff changes future interest and must not be simulated
            # by assigning an aggregate payment an invented date.
            raise ValueError("BORROWING_INSTALLMENT_SETTLEMENT_NOT_SUPPORTED")
        pending_accruals = []
        pending_ids = {}
        for key, plan in self.plans.items():
            if plan.kind != "borrowing_interest_accrual" or plan.derived["borrowing_id"] != str(
                c.borrowing_id
            ):
                continue
            state = plan.derived
            pending_ids[key] = uuid.UUID(state["accrual_id"])
            pending_accruals.append(
                BorrowingInterestAccrual(
                    id=pending_ids[key],
                    borrowing_id=c.borrowing_id,
                    event_id=self.event.id,
                    org_id=self.request.org_id,
                    period_start=date.fromisoformat(state["period_start"]),
                    period_end=date.fromisoformat(state["period_end"]),
                    amount_fen=state["interest_fen"],
                )
            )
        if c.accrual_component_key and c.accrual_component_key not in pending_ids:
            raise ValueError("BORROWING_ACCRUAL_COMPONENT_SOURCE_MISMATCH")
        return compile_borrowing_payment(
            self.session,
            org_id=self.request.org_id,
            posting_date=self.request.posting_date,
            payment_date=c.payment_date,
            key=c.key,
            borrowing_id=c.borrowing_id,
            kind=c.kind,
            accrual_event_id=c.accrual_event_id,
            accrual_id=pending_ids.get(c.accrual_component_key),
            pending_accruals=pending_accruals,
            pending_paid_accrual_ids={
                pending_ids[p.accrual_component_key]
                for p in self.request.components
                if p.kind == "borrowing_interest_payment"
                and p.borrowing_id == c.borrowing_id
                and p.accrual_component_key in pending_ids
            },
            amount_fen=c.amount_fen,
            pending_interest_accrual_event_ids={
                p.accrual_event_id
                for p in self.request.components
                if p.kind == "borrowing_interest_payment" and p.borrowing_id == c.borrowing_id
            },
            facts=c.model_dump(mode="json", exclude={"metadata"}),
        )

    def compile_labor_settlement(self, c) -> ComponentPostingPlan:
        from .domain_components import compile_labor_payment

        self.need(c, "payment_date")
        if c.payment_date is None:
            raise ValueError("LABOR_INSTALLMENT_INCOME_ATTRIBUTION_NOT_SUPPORTED")
        if (
            c.settlement_mode == "gross_paid_without_withholding"
            and not c.withholding_exception_evidence_ids
        ):
            raise MissingFacts([f"components.{c.key}.withholding_exception_evidence_ids"])
        return compile_labor_payment(
            self.session,
            org_id=self.request.org_id,
            payment_date=c.payment_date,
            key=c.key,
            source_open_item_id=c.source_open_item_id,
            source_plan=self.plans.get(c.source_component_key),
            source_open_item_key=c.source_open_item_key,
            pending_event_id=self.event.id,
            amount_fen=c.amount_fen,
            settlement_mode=c.settlement_mode,
            withholding_exception_evidence_ids=c.withholding_exception_evidence_ids,
            evidence_ids=list(self.evidence_ids),
            facts=c.model_dump(mode="json", exclude={"metadata"}),
        )

    def compile_labor_tax_settlement(self, c) -> ComponentPostingPlan:
        from .domain_components import compile_labor_tax_settlement

        self.need(c, "payment_date")
        return compile_labor_tax_settlement(
            self.session,
            org_id=self.request.org_id,
            payment_date=self.first_payment_date(c),
            key=c.key,
            source_open_item_id=c.source_open_item_id,
            source_plan=self.plans.get(c.source_component_key),
            pending_event_id=self.event.id,
            amount_fen=c.amount_fen,
            facts=c.model_dump(mode="json", exclude={"metadata"}),
        )

    def compile_salary_settlement(self, c) -> ComponentPostingPlan:
        from .salary_components import compile_salary_settlement

        self.need(c, "payment_date")
        return compile_salary_settlement(
            self.session,
            self.request.org_id,
            c,
            source_plans=self.plans,
            pending_event_id=self.event.id,
        )

    def compile_payroll_accrual(self, c) -> ComponentPostingPlan:
        self.common._active_component_request = self.request
        plan, evidence_ids = self.common.compile_payroll_accrual_component(
            c,
            planned_regular_plans={
                key: self.plans.get(key) for key in c.regular_payroll_component_keys
            },
        )
        self.common._attach_evidence(self.event, evidence_ids)
        self.evidence_ids.update(evidence_ids)
        return plan

    def compile_labor_remuneration_accrual(self, c) -> ComponentPostingPlan:
        from .labor_remuneration_service import LaborRemunerationService

        service = LaborRemunerationService(self.session)
        service._active_component_request = self.request
        plan, evidence_ids = service.compile_accrual_component(c)
        self.common._attach_evidence(self.event, evidence_ids)
        self.evidence_ids.update(evidence_ids)
        return plan

    def compile_payroll_contribution_supplement(self, c) -> ComponentPostingPlan:
        from .payroll_supplement_components import compile_payroll_supplement

        return compile_payroll_supplement(self, c)

    def validate_salary_settlement_plan(self) -> None:
        from .salary_components import validate_salary_settlement_plans

        validate_salary_settlement_plans(
            self.session,
            self.request.org_id,
            tuple(self.plans.values()),
        )

    def compile_funds(self, organization: Organization) -> None:
        assigned = {key: 0 for key in self.plans}
        counts: dict[str, list[tuple[int, int]]] = {key: [] for key in self.plans}
        allocated_sources: dict[tuple[str, str], int] = {}
        for funds in self.request.funds:
            account = get_account_by_code(self.session, self.request.org_id, funds.account_code)
            cash = account_business_class(account) == "cash"
            if not cash:
                self.common._validate_posting_bank_account(
                    self.request.org_id, account.code, funds.payment_date
                )
            elif funds.bank_transaction_references:
                raise ValueError("CASH_FORBIDS_BANK_REFERENCES")
            sign = 1 if funds.direction == "receipt" else -1
            for allocation in funds.allocations:
                key = allocation.component_key
                plan = self.plans[key]
                payment_date = plan.facts.get("payment_date")
                if payment_date and payment_date != funds.payment_date.isoformat():
                    raise ValueError("COMPONENT_FUNDS_PAYMENT_DATE_MISMATCH")
                self.validate_fund_source_dates(plan, allocation, funds.payment_date)
                plan.derived.setdefault("settlement_schedule", []).append(
                    {
                        "fund_key": funds.key,
                        "payment_date": funds.payment_date.isoformat(),
                        "direction": funds.direction,
                        "amount_fen": allocation.amount_fen,
                        "sources": [
                            source.model_dump(mode="json")
                            for source in allocation.source_allocations
                        ],
                    }
                )
                assigned[key] += sign * allocation.amount_fen
                counts[key].append((sign, allocation.amount_fen))
                parts = plan.derived.get("cash_flow_parts")
                if parts:
                    total = sum(p["amount_fen"] for p in parts)
                    if allocation.source_allocations:
                        if (
                            sum(a.amount_fen for a in allocation.source_allocations)
                            != allocation.amount_fen
                        ):
                            raise ValueError("FUNDS_SOURCE_ALLOCATION_AMOUNT_MISMATCH")
                        selected = []
                        for source_allocation in allocation.source_allocations:
                            source = source_allocation.model_dump(
                                mode="json", exclude={"amount_fen"}
                            )
                            matches = [p for p in parts if p["source"] == source]
                            if len(matches) != 1:
                                raise ValueError("FUNDS_SOURCE_ALLOCATION_NOT_UNIQUE")
                            source_key = (key, payload_hash(source))
                            allocated_sources[source_key] = (
                                allocated_sources.get(source_key, 0) + source_allocation.amount_fen
                            )
                            if allocated_sources[source_key] > matches[0]["amount_fen"]:
                                raise ValueError("FUNDS_SOURCE_ALLOCATION_EXCEEDED")
                            selected.append((matches[0]["category"], source_allocation.amount_fen))
                    elif len({p["category"] for p in parts}) == 1:
                        selected = [(parts[0]["category"], allocation.amount_fen)]
                    elif total == allocation.amount_fen:
                        selected = [(p["category"], p["amount_fen"]) for p in parts]
                    else:
                        raise MissingFacts(
                            [f"funds.{funds.key}.allocations.{key}.source_allocations"]
                        )
                    for category, amount in selected:
                        if amount:
                            plan.cash_flows.append(
                                CashFlowPlan(account.code, category, sign * amount)
                            )
                else:
                    if allocation.source_allocations:
                        raise ValueError("FUNDS_SOURCE_ALLOCATION_REQUIRES_OBLIGATIONS")
                    category = plan.derived.get("cash_flow_category")
                    if category is None:
                        raise ValueError(f"COMPONENT_CASH_FLOW_CLASSIFICATION_REQUIRED:{key}")
                    plan.cash_flows.append(
                        CashFlowPlan(account.code, category, sign * allocation.amount_fen)
                    )

            key = f"funds.{funds.key}"
            self.plans[key] = replace(
                funds_posting_plan(funds.model_dump(mode="json")),
                rule_version=RULE_VERSION,
            )
        for key, amount in assigned.items():
            plan = self.plans[key]
            required = sum(e.credit_fen - e.debit_fen for e in plan.entries)
            if required != amount:
                raise ValueError(f"COMPONENT_FUNDS_CONSERVATION_FAILED:{key}")
            if plan.kind == "funds_transfer":
                target = plan.derived["amount_fen"]
                if (
                    sum(a for s, a in counts[key] if s == 1) != target
                    or sum(a for s, a in counts[key] if s == -1) != target
                ):
                    raise ValueError("FUNDS_TRANSFER_REQUIRES_EQUAL_REAL_MOVEMENTS")
            elif "cash_inflow_fen" in plan.derived and "cash_outflow_fen" in plan.derived:
                for direction, field in ((1, "cash_inflow_fen"), (-1, "cash_outflow_fen")):
                    if sum(a for s, a in counts[key] if s == direction) != plan.derived[field]:
                        raise ValueError("COMPONENT_GROSS_FUNDS_CONSERVATION_FAILED")
            elif any(sign != (1 if required > 0 else -1) for sign, _ in counts[key]):
                raise ValueError("BUSINESS_COMPONENT_FORBIDS_OFFSETTING_FUNDS")

    def validate_fund_source_dates(self, plan, allocation, paid_on):
        """Do not use an obligation before its evidenced recognition cutoff."""
        selected = allocation.source_allocations or plan.settlements
        for reference in selected:
            if reference.open_item_id:
                item = self.session.get(OpenItem, reference.open_item_id)
                if item is None or item.org_id != self.request.org_id:
                    raise ValueError("OPEN_ITEM_NOT_FOUND")
                source = self.session.get(BusinessEventComponent, item.source_component_id)
                facts = source.facts
            else:
                source = self.plans[reference.source_component_key]
                facts = source.facts
            cutoff = recognition_date(facts)
            if cutoff is not None and cutoff > paid_on:
                if facts.get("recognition_period"):
                    source_context = {"component_key": plan.key, "source_component_key": source.key}
                    if reference.open_item_id:
                        source_context |= {
                            "source_event_id": str(source.event_id),
                            "source_component_id": str(source.id),
                            "open_item_id": str(item.id),
                        }
                    raise AccountingFactError(
                        AccountingFactIssue(
                            code="SOURCE_RECOGNITION_BY_PAYMENT_REQUIRED",
                            kind="missing_accounting_fact",
                            fields=["source.business_date", "source.recognition_period"],
                            alternatives=["source.business_date", "source.recognition_period"],
                            actual_values={
                                "source_recognition_period": facts["recognition_period"],
                                "payment_date": paid_on,
                            },
                            expected={"source_recognized_not_after": paid_on},
                            context=source_context,
                            message="现有来源仅证明截至月末债务成立。先核对原来源和证据是否已能证明付款前债务成立；若需更正来源，使用正式更正入口。缺项不是外部申报日或个人实际垫付日，不得补造日期或改动真实资金日。",
                        ),
                        [f"components.{plan.key}.source_recognized_by.{paid_on.isoformat()}"],
                    )
                raise ValueError("SETTLEMENT_SOURCE_NOT_ACTIVE_OR_FUTURE")

    def configure_account(self, request: ConfigureAccountRequest) -> dict:
        with self.session.begin_nested():
            organization = self.session.scalar(
                select(Organization)
                .where(
                    Organization.id == request.org_id,
                )
                .with_for_update()
            )
            if organization is None:
                raise ValueError("ORGANIZATION_NOT_FOUND")
            payload = request.model_dump(mode="json")
            previous = [
                row
                for row in self.session.scalars(
                    select(AuditLog).where(
                        AuditLog.org_id == request.org_id,
                        AuditLog.action == "account_configured",
                    )
                )
                if row.details.get("idempotency_key") == request.idempotency_key
            ]
            if any(row.details != payload for row in previous):
                raise ValueError("IDEMPOTENCY_KEY_PAYLOAD_MISMATCH")
            default = get_business_class_template(
                self.session, request.org_id, request.business_class
            )
            account = self.session.scalar(
                select(Account).where(
                    Account.org_id == request.org_id,
                    Account.code == request.code,
                )
            )
            if account is not None:
                if (account.name, account_business_class(account)) != (
                    request.name,
                    request.business_class,
                ):
                    raise ValueError("ACCOUNT_CONFIGURATION_CONFLICT")
            else:
                account = Account(
                    org_id=request.org_id,
                    code=request.code,
                    name=request.name,
                    category=default.category,
                    normal_side=default.normal_side,
                    business_class=request.business_class,
                )
                self.session.add(account)
                self.session.flush()
            if not previous:
                self.session.add(
                    AuditLog(org_id=request.org_id, action="account_configured", details=payload)
                )
            return {
                "status": "configured",
                "account_id": str(account.id),
                "code": account.code,
                "business_class": account_business_class(account),
            }
