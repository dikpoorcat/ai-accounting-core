"""Supplier advances and traceable project costs, compiled without formal writes."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from .coa import account_business_class, get_account_by_code, get_account_by_role
from .domain_action_schemas import DomainFactsRequired, domain_request
from .intangible_asset_service import IntangibleAssetService
from .ledger import Entry, OpenItemPlan, SettlementPlan
from .models import BusinessEvent, BusinessEventComponent, VoucherLine

RULE_SOURCE = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf"
COST_SOURCE = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
PURPOSE_CASH_LINES = {
    "goods_or_services": 3,
    "operating_expense": 6,
    "fixed_asset": 12,
    "intangible_asset": 12,
}
COST_ROLES = {
    "purchased_intangible": "intangible_project_cost",
    "internal_development": "development_expenditure",
}


def _need(service, c, *fields):
    service.need(c, *fields)
    if any(isinstance(getattr(c, f), str) and not getattr(c, f).strip() for f in fields):
        raise ValueError("PURCHASE_BUSINESS_REFERENCE_MUST_NOT_BE_BLANK")


def _date(service, c, *, payment=False):
    if c.business_date > service.request.posting_date:
        raise ValueError("PURCHASE_BUSINESS_DATE_IN_FUTURE")
    if payment:
        _need(service, c, "payment_date")
        if c.payment_date > service.request.posting_date:
            raise ValueError("PURCHASE_PAYMENT_DATE_IN_FUTURE")


def compile_supplier_advance(service, c):
    _need(service, c, "purchase_purpose")
    _date(service, c, payment=True)
    return service.plan(
        c,
        [Entry(account_role="prepayments", debit_fen=c.amount_fen, counterparty_id=None)],
        open_items=[
            OpenItemPlan(
                counterparty_id=None,
                item_type="receivable",
                original_amount_fen=c.amount_fen,
                account_role="prepayments",
            )
        ],
        derived={
            "amount_fen": c.amount_fen,
            "open_item_accounts": {"primary": "prepayments"},
            "accounting_rule_source_url": RULE_SOURCE,
        },
        cash_line=PURPOSE_CASH_LINES[c.purchase_purpose],
    )


def _settle_sources(service, c, allocations, *, advance):
    entries, settlements, parts = [], [], []
    seen = set()
    for a in allocations:
        identity = (a.open_item_id, a.source_component_key, a.source_open_item_key)
        if identity in seen:
            raise ValueError("DUPLICATE_OBLIGATION_SETTLEMENT_SOURCE")
        seen.add(identity)
        item, facts, derived, key = service.obligation(a)
        expected = "receivable" if advance else "payable"
        if item.item_type != expected:
            raise ValueError("PURCHASE_SETTLEMENT_SUPPLIER_OR_DIRECTION_MISMATCH")
        role, code = service.obligation_account(item, derived, key)
        account = (
            get_account_by_role(service.session, service.request.org_id, role)
            if role
            else get_account_by_code(service.session, service.request.org_id, code)
        )
        if advance:
            if (
                facts.get("kind") != "supplier_advance"
                or account_business_class(account) != "prepayments"
            ):
                raise ValueError("SUPPLIER_ADVANCE_SOURCE_REQUIRED")
        elif account_business_class(account) != "accounts_payable" or item.payable_category:
            raise ValueError("SUPPLIER_TRADE_PAYABLE_SOURCE_REQUIRED")
        entries.append(
            Entry(
                account_code=account.code,
                counterparty_id=item.counterparty_id,
                debit_fen=0 if advance else a.amount_fen,
                credit_fen=a.amount_fen if advance else 0,
            )
        )
        settlements.append(
            SettlementPlan(
                amount_fen=a.amount_fen,
                purpose=c.kind,
                expected_item_type=expected,
                counterparty_id=item.counterparty_id,
                open_item_id=a.open_item_id,
                source_component_key=a.source_component_key,
                source_open_item_key=a.source_open_item_key,
            )
        )
        if advance:
            parts.append(
                {
                    "amount_fen": a.amount_fen,
                    "category": derived["cash_flow_category"],
                    "source": a.model_dump(mode="json", exclude={"amount_fen"}),
                }
            )
    return entries, settlements, parts


def compile_supplier_advance_application(service, c):
    _date(service, c)
    total = sum(a.amount_fen for a in c.advances)
    if total != sum(a.amount_fen for a in c.allocations):
        raise ValueError("SUPPLIER_ADVANCE_APPLICATION_TOTAL_MISMATCH")
    debit, payables, _ = _settle_sources(service, c, c.allocations, advance=False)
    credit, advances, _ = _settle_sources(service, c, c.advances, advance=True)
    return service.plan(
        c,
        debit + credit,
        settlements=advances + payables,
        derived={"amount_fen": total, "accounting_rule_source_url": RULE_SOURCE},
    )


def compile_supplier_advance_refund(service, c):
    _date(service, c, payment=True)
    entries, settlements, parts = _settle_sources(service, c, c.advances, advance=True)
    return service.plan(
        c,
        entries,
        settlements=settlements,
        derived={"cash_flow_parts": parts, "accounting_rule_source_url": RULE_SOURCE},
    )


def compile_project_cost(service, c):
    _need(
        service,
        c,
        "project_nature",
        "cost_element",
        "rights_controlled",
    )
    _date(service, c)
    if not c.rights_controlled:
        raise ValueError("PROJECT_COST_REQUIRES_CONTROLLED_STAGE_RESULT")
    if c.project_nature == "internal_development":
        _need(service, c, "development_conditions")
        from .component_service import MissingFacts

        missing = [
            f"components.{c.key}.development_conditions.{f}"
            for f in type(c.development_conditions).model_fields
            if getattr(c.development_conditions, f) is None
        ]
        if missing:
            raise MissingFacts(missing)
        conditions = c.development_conditions.model_dump()
        met = conditions.pop("conditions_met_date")
        if met > c.business_date or not all(conditions.values()):
            raise ValueError("PROJECT_DEVELOPMENT_CAPITALIZATION_CONDITIONS_NOT_MET")
        if c.cost_element == "purchase_price":
            raise ValueError("DEVELOPMENT_COST_REQUIRES_ATTRIBUTABLE_COST_OR_TAX")
    elif c.development_conditions is not None:
        raise ValueError("DEVELOPMENT_CONDITIONS_REQUIRE_DEVELOPMENT_PROJECT")
    role = COST_ROLES[c.project_nature]
    return service.plan(
        c,
        [
            Entry(account_role=role, debit_fen=c.amount_fen, counterparty_id=None),
            Entry(account_role="accounts_payable", credit_fen=c.amount_fen, counterparty_id=None),
        ],
        open_items=[
            OpenItemPlan(
                counterparty_id=None,
                item_type="payable",
                original_amount_fen=c.amount_fen,
                account_role="accounts_payable",
            )
        ],
        derived={
            "amount_fen": c.amount_fen,
            "cost_role": role,
            "open_item_accounts": {"primary": "accounts_payable"},
            "accounting_rule_source_url": COST_SOURCE,
        },
        cash_line=12,
    )


def _source_identity(service, ref, event_id=None):
    if ref.get("component_id"):
        source = service.session.get(BusinessEventComponent, uuid.UUID(str(ref["component_id"])))
        return (source.event_id, source.key) if source else None
    return (event_id or service.event.id, ref.get("component_key"))


def project_cost_balances(session, org_id, components):
    """Read current unused eligible cost; reversed consumers release their sources."""
    parents = {str(c.id): c for c in components if c.kind == "project_cost"}
    if not parents:
        return {}
    used = {identity: 0 for identity in parents}
    local = {(c.event_id, c.key): str(c.id) for c in parents.values()}
    for child in session.scalars(
        select(BusinessEventComponent)
        .join(BusinessEvent, BusinessEvent.id == BusinessEventComponent.event_id)
        .where(BusinessEventComponent.org_id == org_id, BusinessEvent.status == "posted")
    ):
        for source in child.facts.get("cost_sources", []):
            identity = source.get("component_id") or local.get(
                (child.event_id, source.get("component_key"))
            )
            if identity in used:
                used[identity] += source["amount_fen"]
    return {
        identity: {
            "recognized_fen": c.facts["amount_fen"],
            "consumed_fen": used[identity],
            "available_fen": c.facts["amount_fen"] - used[identity]
            if session.get(BusinessEvent, c.event_id).status == "posted"
            else 0,
        }
        for identity, c in parents.items()
    }


def project_cost_sources(service, c):
    """Consume only explicit eligible cost sources; lock and count all active uses."""
    _date(service, c)
    entries, effects, uses = [], [], []
    totals = {
        "purchase_price_fen": 0,
        "noncreditable_tax_fen": 0,
        "directly_attributable_cost_fen": 0,
    }
    active = service.session.scalars(
        select(BusinessEventComponent)
        .join(BusinessEvent, BusinessEvent.id == BusinessEventComponent.event_id)
        .where(
            BusinessEventComponent.org_id == service.request.org_id,
            BusinessEvent.status == "posted",
        )
    ).all()
    seen = set()
    for allocation in c.cost_sources:
        ref = allocation.model_dump(mode="json", exclude={"amount_fen"}, exclude_none=True)
        # The shared resolver enforces company, source kind, active state and dates.
        facts, derived, source_event_id = service.source(
            SimpleNamespace(source=allocation), {"project_cost"}
        )
        identity = _source_identity(service, allocation.model_dump(exclude={"amount_fen"}))
        if identity in seen:
            raise ValueError("DUPLICATE_PROJECT_COST_SOURCE")
        seen.add(identity)
        if allocation.component_id:
            rows = service.session.scalars(
                select(VoucherLine).where(
                    VoucherLine.component_id == allocation.component_id, VoucherLine.debit_fen > 0
                )
            ).all()
            accounts = {
                row.account.code
                for row in rows
                if account_business_class(row.account) == derived["cost_role"]
            }
        else:
            accounts = {
                entry.account_code
                or get_account_by_role(
                    service.session, service.request.org_id, entry.account_role
                ).code
                for entry in service.plans[allocation.component_key].entries
                if entry.debit_fen
            }
        if len(accounts) != 1:
            raise ValueError("PROJECT_COST_ACCOUNT_ORIGIN_AMBIGUOUS")
        used = 0
        for row in [*active, *service.plans.values()]:
            for use in row.facts.get("cost_sources", []):
                if _source_identity(service, use, getattr(row, "event_id", None)) == identity:
                    used += use["amount_fen"]
        if used + allocation.amount_fen > facts["amount_fen"]:
            raise ValueError("PROJECT_COST_SOURCE_AMOUNT_EXCEEDED")
        code = next(iter(accounts))
        entries.append(Entry(account_code=code, credit_fen=allocation.amount_fen))
        totals[f"{facts['cost_element']}_fen"] += allocation.amount_fen
        uses.append(
            {
                "source": ref,
                "amount_fen": allocation.amount_fen,
                "account_code": code,
                "cost_element": facts["cost_element"],
            }
        )
        effects.append(
            service.source_dependency(
                source_event_id, allocation.component_id, allocation.amount_fen
            )
        )
    return entries, effects, uses, totals


def compile_project_cost_expense(service, c):
    _need(service, c, "expense_class")
    credits, effects, uses, totals = project_cost_sources(service, c)
    return service.plan(
        c,
        [Entry(account_role=c.expense_class, debit_fen=sum(totals.values())), *credits],
        effects=effects,
        derived={"cost_uses": uses, "accounting_rule_source_url": COST_SOURCE},
    )


def compile_intangible_asset_acquisition(service, c):
    from .component_service import MissingFacts

    facts = c.facts
    source_plan = None
    if getattr(facts, "settlement_method", None) == "project_cost":
        if not c.cost_sources:
            raise MissingFacts([f"components.{c.key}.cost_sources"])
        source_plan = project_cost_sources(service, c)
        totals = source_plan[3]
        total = sum(totals.values())
        if facts.cost_fen is not None and facts.cost_fen != total:
            raise ValueError("INTANGIBLE_ASSET_PROJECT_COST_TOTAL_MISMATCH")
        if facts.cost_components is not None:
            for name, amount in facts.cost_components.model_dump().items():
                if amount is not None and amount != totals[name]:
                    raise ValueError("INTANGIBLE_ASSET_PROJECT_COST_BREAKDOWN_MISMATCH")
        from .intangible_asset_schemas import IntangibleAssetCostComponents

        facts = facts.model_copy(
            update={"cost_fen": total, "cost_components": IntangibleAssetCostComponents(**totals)}
        )
    try:
        request = domain_request(
            c.kind,
            facts,
            org_id=service.request.org_id,
            key=c.key,
            posting_date=service.request.posting_date,
            business_date=c.business_date,
            payment_date=c.payment_date,
            evidence_references=list(service.evidence_ids),
            description="",
        )
    except DomainFactsRequired as exc:
        raise MissingFacts([f"components.{c.key}.facts.{f}" for f in exc.fields]) from exc
    if request.settlement_method.value != "project_cost":
        if c.cost_sources:
            raise ValueError("PROJECT_COST_SOURCES_REQUIRE_PROJECT_COST_SETTLEMENT")
        return IntangibleAssetService(service.session).compile_acquisition(request, key=c.key)
    credits, effects, uses, totals = source_plan
    plan = IntangibleAssetService(service.session).compile_acquisition(
        request, key=c.key, project_cost_entries=credits
    )
    plan.facts["cost_sources"] = [a.model_dump(mode="json") for a in c.cost_sources]
    plan.derived.update(cost_uses=uses)
    plan.effects.extend(effects)
    return plan
