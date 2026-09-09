from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .accounting_periods import china_current_date
from .coa import get_account_by_code, get_account_by_role
from .models import (
    AccountingPeriod,
    AuditLog,
    BusinessEvent,
    BusinessEventComponent,
    ComponentCashFlowAllocation,
    OpenItem,
    Organization,
    Settlement,
    Voucher,
    VoucherLine,
    VoucherSequence,
)


class AccountingPeriodError(ValueError):
    """Stable period-control rejection raised at the common posting boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_business_event(session: Session, **facts: Any) -> BusinessEvent:
    """Reuse the locked event only inside an audited open-month amendment."""
    amendment = session.info.get("event_amendment")
    if amendment is None:
        return BusinessEvent(**facts)
    original = amendment["event"]
    if (
        amendment.get("event_built")
        or original.status != "draft"
        or facts["org_id"] != original.org_id
        or facts["event_type"] != original.event_type
    ):
        raise ValueError("AMENDMENT_EVENT_TYPE_MISMATCH")
    amendment["event_built"] = True
    for key, value in facts.items():
        if key not in {"id", "idempotency_key", "request_payload_hash", "execution_attribution_id"}:
            setattr(original, key, value)
    return original


@dataclass(frozen=True)
class Entry:
    debit_fen: int = 0
    credit_fen: int = 0
    account_role: str | None = None
    account_code: str | None = None
    counterparty_id: uuid.UUID | None = None
    memo: str = ""
    component_id: uuid.UUID | None = None

    def validate(self) -> None:
        if type(self.debit_fen) is not int or type(self.credit_fen) is not int:
            raise ValueError("entry amounts must be integer fen")
        if (self.account_role is None) == (self.account_code is None):
            raise ValueError("entry requires exactly one of account_role or account_code")
        if self.debit_fen < 0 or self.credit_fen < 0:
            raise ValueError("entry amounts cannot be negative")
        if (self.debit_fen > 0) == (self.credit_fen > 0):
            raise ValueError("entry must contain exactly one positive side")


@dataclass(frozen=True)
class OpenItemPlan:
    """An internal-only receivable/payable created by a deterministic posting plan."""

    counterparty_id: uuid.UUID | None
    item_type: str
    original_amount_fen: int
    due_date: date | None = None
    payable_category: str | None = None
    payable_agency_code: str | None = None
    insurance_kind: str | None = None
    key: str = "primary"
    pass_through_key: str | None = None
    pass_through_beneficiary_id: uuid.UUID | None = None
    account_role: str | None = None
    account_code: str | None = None

    def validate(self) -> None:
        if self.account_role is not None and self.account_code is not None:
            raise ValueError("open item account requires at most one selector")
        if self.item_type not in {"receivable", "payable"}:
            raise ValueError("open item type must be receivable or payable")
        if type(self.original_amount_fen) is not int or self.original_amount_fen <= 0:
            raise ValueError("open item amount must be positive")
        if not self.key.strip():
            raise ValueError("open item key is required")
        if self.payable_category is None:
            if self.payable_agency_code is not None or self.insurance_kind is not None:
                raise ValueError("payable target metadata requires a payable category")
            return
        if self.item_type != "payable":
            raise ValueError("payable category is only valid for payable open items")
        if self.payable_category not in {
            "salary",
            "employer_social",
            "withheld_employee_social",
            "employer_housing",
            "withheld_employee_housing",
            "individual_income_tax",
            "labor_remuneration",
            "labor_individual_income_tax",
            "pass_through",
        }:
            raise ValueError(f"unsupported payable category: {self.payable_category}")
        if self.payable_category == "pass_through" and (not self.pass_through_key):
            raise ValueError("pass through payable requires its stable business key")
        if self.payable_category != "pass_through" and (
            self.pass_through_key is not None or self.pass_through_beneficiary_id is not None
        ):
            raise ValueError("pass through metadata requires pass through category")
        if (
            self.payable_category
            in {
                "employer_social",
                "withheld_employee_social",
                "employer_housing",
                "withheld_employee_housing",
            }
            and self.insurance_kind is None
        ):
            raise ValueError("statutory payable requires insurance kind")


def create_open_items(
    session: Session,
    *,
    event: BusinessEvent,
    plans: list[OpenItemPlan],
    component: BusinessEventComponent | None = None,
) -> list[OpenItem]:
    """Persist every internally-derived open item for one posted business event.

    Public requests never supply this plan.  It enables fixed internal posting templates,
    such as payroll accrual, to create several auditable payables atomically.
    """

    if not plans:
        return []
    open_items: list[OpenItem] = []
    for plan in plans:
        plan.validate()
        account = (
            get_account_by_role(session, event.org_id, plan.account_role)
            if plan.account_role is not None
            else (
                get_account_by_code(session, event.org_id, plan.account_code)
                if plan.account_code is not None
                else None
            )
        )
        open_item = OpenItem(
            org_id=event.org_id,
            counterparty_id=plan.counterparty_id,
            source_event_id=event.id,
            item_type=plan.item_type,
            original_amount_fen=plan.original_amount_fen,
            due_date=plan.due_date,
            payable_category=plan.payable_category,
            payable_agency_code=plan.payable_agency_code,
            insurance_kind=plan.insurance_kind,
            source_component_id=component.id if component else None,
            component_key=plan.key if component else None,
            pass_through_key=plan.pass_through_key,
            pass_through_beneficiary_id=plan.pass_through_beneficiary_id,
            account_id=account.id if account else None,
        )
        session.add(open_item)
        open_items.append(open_item)
    session.flush()
    return open_items


@dataclass(frozen=True)
class SettlementPlan:
    amount_fen: int
    purpose: str
    expected_item_type: str
    counterparty_id: uuid.UUID | None
    open_item_id: uuid.UUID | None = None
    source_component_key: str | None = None
    source_open_item_key: str = "primary"

    def validate(self) -> None:
        if type(self.amount_fen) is not int or self.amount_fen <= 0:
            raise ValueError("settlement amount must be positive integer fen")
        if (self.open_item_id is None) == (self.source_component_key is None):
            raise ValueError("settlement requires exactly one source")
        if self.expected_item_type not in {"receivable", "payable"} or not self.purpose.strip():
            raise ValueError("settlement requires typed purpose and direction")


@dataclass(frozen=True)
class CashFlowPlan:
    bank_account_code: str
    category: str
    amount_fen: int


@dataclass(frozen=True)
class ComponentPostingPlan:
    """Internal compiler output; never an input accepted by a public tool."""

    key: str
    kind: str
    facts: dict[str, Any]
    entries: list[Entry]
    derived: dict[str, Any] = field(default_factory=dict)
    rule_version: str | None = None
    open_items: list[OpenItemPlan] = field(default_factory=list)
    settlements: list[SettlementPlan] = field(default_factory=list)
    cash_flows: list[CashFlowPlan] = field(default_factory=list)
    effects: list[Callable[[Session, BusinessEvent, BusinessEventComponent], None]] = field(
        default_factory=list
    )


def funds_posting_plan(facts: dict[str, Any]) -> ComponentPostingPlan:
    """Compile typed settlement facts for component and calculator workflows."""
    from .component_schemas import FundsSettlement

    funds = FundsSettlement.model_validate(facts)
    return ComponentPostingPlan(
        key=f"funds.{funds.key}",
        kind="funds",
        facts=funds.model_dump(mode="json"),
        entries=[
            Entry(
                account_code=funds.account_code,
                debit_fen=funds.amount_fen if funds.direction == "receipt" else 0,
                credit_fen=funds.amount_fen if funds.direction == "payment" else 0,
            )
        ],
    )


def commit_posting_plan(
    session: Session,
    *,
    event: BusinessEvent,
    components: list[ComponentPostingPlan],
    posting_date: date,
    description: str,
    reversal_of: Voucher | None = None,
    existing_voucher: Voucher | None = None,
) -> Voucher:
    """Atomically persist one compiled fact graph and its sole balanced voucher.

    The caller owns the outer transaction and request-level idempotency. This
    savepoint ensures a failed component cannot leave a partially posted graph.
    """
    if event.status != "draft" or not components:
        raise ValueError("POSTING_PLAN_REQUIRES_DRAFT_AND_COMPONENTS")
    if len({plan.key for plan in components}) != len(components):
        raise ValueError("DUPLICATE_COMPONENT_KEY")
    for plan in components:
        if not plan.key.strip() or not plan.kind.strip():
            raise ValueError("COMPONENT_KEY_AND_KIND_REQUIRED")
        if len({item.key for item in plan.open_items}) != len(plan.open_items):
            raise ValueError("DUPLICATE_COMPONENT_OPEN_ITEM_KEY")
        for item in plan.open_items:
            item.validate()
        for settlement in plan.settlements:
            settlement.validate()
    assert_period_open(session, event.org_id, posting_date)
    with session.begin_nested():
        session.add(event)
        session.flush()
        if (
            session.scalar(
                select(BusinessEventComponent.id).where(BusinessEventComponent.event_id == event.id)
            )
            is not None
        ):
            raise ValueError("POSTING_PLAN_ALREADY_MATERIALIZED")
        from .domain_accounts import bind_domain_account_origins

        bind_domain_account_origins(session, event, components)
        from .bank_matching import commit_bank_matches

        commit_bank_matches(session, event, components)
        materialized: dict[str, BusinessEventComponent] = {}
        entries: list[Entry] = []
        for ordinal, plan in enumerate(components, 1):
            component = BusinessEventComponent(
                org_id=event.org_id,
                event_id=event.id,
                key=plan.key,
                ordinal=ordinal,
                kind=plan.kind,
                facts=plan.facts,
                derived=plan.derived,
                rule_version=plan.rule_version,
            )
            session.add(component)
            session.flush()
            materialized[plan.key] = component
            entries.extend(replace(entry, component_id=component.id) for entry in plan.entries)
        created_items: dict[tuple[str, str], OpenItem] = {}
        for plan in components:
            component = materialized[plan.key]
            for item in create_open_items(
                session,
                event=event,
                plans=plan.open_items,
                component=component,
            ):
                created_items[(plan.key, item.component_key)] = item
        # Resolve and lock existing sources in a stable order across all components.
        source_ids = sorted(
            {s.open_item_id for p in components for s in p.settlements if s.open_item_id},
            key=str,
        )
        existing_items = {
            item.id: item
            for item in session.scalars(
                select(OpenItem)
                .where(OpenItem.org_id == event.org_id, OpenItem.id.in_(source_ids))
                .order_by(OpenItem.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        for plan in components:
            component = materialized[plan.key]
            seen_sources: set[uuid.UUID] = set()
            for settlement in plan.settlements:
                item = (
                    existing_items.get(settlement.open_item_id)
                    if settlement.open_item_id is not None
                    else created_items.get(
                        (
                            settlement.source_component_key,
                            settlement.source_open_item_key,
                        )
                    )
                )
                if item is None:
                    raise ValueError("SETTLEMENT_SOURCE_NOT_FOUND")
                if item.id in seen_sources:
                    raise ValueError("DUPLICATE_COMPONENT_SETTLEMENT_SOURCE")
                seen_sources.add(item.id)
                if item.item_type != settlement.expected_item_type:
                    raise ValueError("SETTLEMENT_SOURCE_DIRECTION_MISMATCH")
                if item.counterparty_id != settlement.counterparty_id:
                    raise ValueError("SETTLEMENT_SOURCE_COUNTERPARTY_MISMATCH")
                source = session.get(BusinessEvent, item.source_event_id)
                from .fact_dates import recognition_date

                source_component = session.get(BusinessEventComponent, item.source_component_id)
                if source_component and source_component.facts.get("recognition_period"):
                    if recognition_date(source_component.facts) > (
                        recognition_date(plan.facts) or posting_date
                    ):
                        raise ValueError("MONTHLY_SOURCE_NOT_RECOGNIZED_BY_SETTLEMENT")
                if (
                    source is None
                    or source.org_id != event.org_id
                    or (source.id != event.id and source.status != "posted")
                    or source.posting_date > posting_date
                ):
                    raise ValueError("SETTLEMENT_SOURCE_NOT_ACTIVE_OR_FUTURE")
                if item.status not in {"open", "partial"} or (
                    item.settled_amount_fen + settlement.amount_fen > item.original_amount_fen
                ):
                    raise ValueError("SETTLEMENT_EXCEEDS_OPEN_BALANCE")
                session.add(
                    Settlement(
                        org_id=event.org_id,
                        open_item_id=item.id,
                        payment_event_id=event.id,
                        payment_component_id=component.id,
                        purpose=settlement.purpose,
                        amount_fen=settlement.amount_fen,
                    )
                )
                item.settled_amount_fen += settlement.amount_fen
                item.status = (
                    "settled" if item.settled_amount_fen == item.original_amount_fen else "partial"
                )
            for cash_flow in plan.cash_flows:
                if type(cash_flow.amount_fen) is not int or cash_flow.amount_fen == 0:
                    raise ValueError("CASH_FLOW_REQUIRES_NONZERO_INTEGER_FEN")
                if not cash_flow.category.strip():
                    raise ValueError("CASH_FLOW_CATEGORY_REQUIRED")
                account = get_account_by_code(session, event.org_id, cash_flow.bank_account_code)
                if not account.requires_bank_reconciliation and (
                    account.business_class or account.system_role
                ) not in {"bank", "cash", "payment_platform_funds"}:
                    raise ValueError("CASH_FLOW_REQUIRES_FUNDS_ACCOUNT")
                session.add(
                    ComponentCashFlowAllocation(
                        org_id=event.org_id,
                        event_id=event.id,
                        component_id=component.id,
                        bank_account_id=account.id,
                        category=cash_flow.category,
                        amount_fen=cash_flow.amount_fen,
                    )
                )
        session.flush()
        for plan in components:
            for effect in plan.effects:
                effect(session, event, materialized[plan.key])
        session.flush()
        _assign_open_item_accounts(session, event, entries)
        _snapshot_component_plan(session, event, materialized, entries)
        voucher = create_voucher(
            session,
            event=event,
            posting_date=posting_date,
            description=description,
            entries=entries,
            reversal_of=reversal_of,
            existing_voucher=existing_voucher,
        )
        event.rule_trace = list(event.rule_trace or []) + [
            {
                "stage": "entries_created",
                "debit_fen": sum(e.debit_fen for e in entries),
                "credit_fen": sum(e.credit_fen for e in entries),
                "component_keys": [p.key for p in components],
            }
        ]
        # PostgreSQL deliberately permits draft -> posted only as a status-only
        # transition. Persist the complete graph and its trace while it is still
        # draft, then finalize it in the following statement.
        session.flush()
        event.status = "posted"
        session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="event_posted",
                details={
                    "voucher_id": str(voucher.id),
                    "voucher_number": voucher.voucher_number,
                    "component_keys": [p.key for p in components],
                },
            )
        )
        session.flush()
        return voucher


def _assign_open_item_accounts(
    session: Session, event: BusinessEvent, entries: list[Entry]
) -> None:
    """Attach a calculator's exact receivable/payable account to its source item."""
    category_roles = {
        "salary": "employee_salary_payable",
        "employer_social": "employer_social_payable",
        "employer_housing": "employer_housing_fund_payable",
        "withheld_employee_social": "withheld_employee_social_payable",
        "withheld_employee_housing": "withheld_employee_housing_fund_payable",
        "individual_income_tax": "individual_income_tax_payable",
        "labor_individual_income_tax": "individual_income_tax_payable",
        "labor_remuneration": "labor_remuneration_payable",
        "pass_through": "pass_through_payable",
    }
    resolved = [
        (
            entry,
            get_account_by_role(session, event.org_id, entry.account_role)
            if entry.account_role
            else get_account_by_code(session, event.org_id, entry.account_code),
        )
        for entry in entries
    ]
    for item in session.scalars(
        select(OpenItem).where(OpenItem.source_event_id == event.id, OpenItem.account_id.is_(None))
    ):
        expected_role = category_roles.get(item.payable_category)
        candidates = {
            account.id
            for entry, account in resolved
            if entry.component_id == item.source_component_id
            and entry.counterparty_id in {None, item.counterparty_id}
            and (entry.debit_fen > 0 if item.item_type == "receivable" else entry.credit_fen > 0)
            and not account.requires_bank_reconciliation
            and (account.business_class or account.system_role)
            not in {"bank", "cash", "payment_platform_funds"}
            and (
                not expected_role
                or (account.business_class or account.system_role) == expected_role
            )
            and account.category == ("asset" if item.item_type == "receivable" else "liability")
        }
        if len(candidates) != 1:
            raise ValueError("OPEN_ITEM_ACCOUNT_ATTRIBUTION_REQUIRED")
        item.account_id = candidates.pop()
    session.flush()


def _snapshot_component_plan(
    session: Session,
    event: BusinessEvent,
    components: dict[str, BusinessEventComponent],
    entries: list[Entry],
) -> None:
    """Persist compiler output for database checks and deterministic replay review."""
    for component in components.values():
        compiled_entries = []
        for entry in entries:
            if entry.component_id != component.id:
                continue
            account = (
                get_account_by_role(session, event.org_id, entry.account_role)
                if entry.account_role
                else get_account_by_code(
                    session,
                    event.org_id,
                    entry.account_code,
                )
            )
            compiled_entries.append(
                {
                    "account_id": str(account.id),
                    "counterparty_id": str(entry.counterparty_id)
                    if entry.counterparty_id
                    else None,
                    "debit_fen": entry.debit_fen,
                    "credit_fen": entry.credit_fen,
                }
            )
        source_items = list(
            session.scalars(
                select(OpenItem)
                .where(
                    OpenItem.source_component_id == component.id,
                )
                .order_by(OpenItem.component_key)
            )
        )
        settlements = list(
            session.scalars(
                select(Settlement)
                .where(
                    Settlement.payment_component_id == component.id,
                )
                .order_by(Settlement.open_item_id)
            )
        )
        flows = list(
            session.scalars(
                select(ComponentCashFlowAllocation)
                .where(
                    ComponentCashFlowAllocation.component_id == component.id,
                )
                .order_by(ComponentCashFlowAllocation.id)
            )
        )
        component.derived = dict(component.derived) | {
            "_posting_entries": compiled_entries,
            "_posting_open_items": [
                {
                    "key": item.component_key,
                    "account_id": str(item.account_id),
                    "counterparty_id": str(item.counterparty_id) if item.counterparty_id else None,
                    "item_type": item.item_type,
                    "amount_fen": item.original_amount_fen,
                }
                for item in source_items
            ],
            "_posting_settlements": [
                {
                    "open_item_id": str(item.open_item_id),
                    "amount_fen": item.amount_fen,
                    "purpose": item.purpose,
                }
                for item in settlements
            ],
            "_posting_cash_flows": [
                {
                    "bank_account_id": str(item.bank_account_id),
                    "category": item.category,
                    "amount_fen": item.amount_fen,
                }
                for item in flows
            ],
        }
    session.flush()


def posting_period_error_code(
    session: Session,
    org_id: uuid.UUID,
    posting_date: date,
    *,
    current_date: date | None = None,
) -> str | None:
    """Return the stable posting-period error without locks or writes."""

    if posting_date > (current_date or china_current_date()):
        return "ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"
    organization = session.get(Organization, org_id)
    if organization is None:
        return "ORGANIZATION_NOT_FOUND"
    periods = list(
        session.scalars(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.start_date <= posting_date,
                AccountingPeriod.end_date >= posting_date,
            )
        ).all()
    )
    if any(period.status == "closed" for period in periods):
        return "ACCOUNTING_PERIOD_CLOSED"
    if bool(getattr(organization, "accounting_period_control_enabled", False)):
        control_start = getattr(organization, "accounting_period_control_start_date", None)
        if control_start is None or posting_date < control_start:
            return "ACCOUNTING_PERIOD_NOT_GENERATED"
        if len(periods) != 1 or periods[0].status != "open":
            return "ACCOUNTING_PERIOD_NOT_GENERATED"
    return None


def assert_period_open(session: Session, org_id: uuid.UUID, posting_date: date) -> None:
    today = china_current_date()
    if posting_date > today:
        raise AccountingPeriodError("ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED")
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'accounting_period:' || CAST(:org_id AS text) || ':' || "
                "CAST(date_trunc('month', CAST(:posting_date AS date))::date AS text), 0))"
            ),
            {"org_id": str(org_id), "posting_date": posting_date},
        )
    if code := posting_period_error_code(session, org_id, posting_date, current_date=today):
        raise AccountingPeriodError(code)


def _next_voucher_number(session: Session, org_id: uuid.UUID, posting_date: date) -> str:
    period_key = posting_date.strftime("%Y%m")
    dialect = session.get_bind().dialect.name
    values = {"org_id": org_id, "period_key": period_key, "next_number": 1}
    if dialect == "postgresql":
        session.execute(
            pg_insert(VoucherSequence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["org_id", "period_key"])
        )
    elif dialect == "sqlite":
        session.execute(
            sqlite_insert(VoucherSequence)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["org_id", "period_key"])
        )
    else:
        existing = session.get(VoucherSequence, (org_id, period_key))
        if existing is None:
            session.add(VoucherSequence(**values))
            session.flush()

    sequence = session.scalar(
        select(VoucherSequence)
        .where(
            VoucherSequence.org_id == org_id,
            VoucherSequence.period_key == period_key,
        )
        .with_for_update()
    )
    if sequence is None:
        raise RuntimeError("failed to initialize voucher sequence")
    number = sequence.next_number
    sequence.next_number += 1
    return f"{period_key}-{number:04d}"


def create_voucher(
    session: Session,
    *,
    event: BusinessEvent,
    posting_date: date,
    description: str,
    entries: list[Entry],
    reversal_of: Voucher | None = None,
    existing_voucher: Voucher | None = None,
) -> Voucher:
    if len(entries) < 2:
        raise ValueError("a voucher requires at least two lines")
    for entry in entries:
        entry.validate()
    debit_total = sum(entry.debit_fen for entry in entries)
    credit_total = sum(entry.credit_fen for entry in entries)
    if debit_total != credit_total:
        raise ValueError(f"unbalanced voucher: debit={debit_total}, credit={credit_total}")
    if debit_total <= 0:
        raise ValueError("voucher total must be positive")

    assert_period_open(session, event.org_id, posting_date)
    amendment = session.info.get("event_amendment")
    if amendment is not None:
        existing_voucher = amendment["voucher"]
    if existing_voucher is not None and (
        existing_voucher.status != "draft"
        or existing_voucher.event_id != event.id
        or existing_voucher.org_id != event.org_id
        or existing_voucher.posting_date.strftime("%Y%m") != posting_date.strftime("%Y%m")
        or reversal_of is not None
    ):
        raise ValueError("INVALID_AMENDMENT_VOUCHER")
    voucher = existing_voucher or Voucher(
        org_id=event.org_id,
        event_id=event.id,
        voucher_number=_next_voucher_number(session, event.org_id, posting_date),
        posting_date=posting_date,
        description=description,
        status="draft",
        reversal_of_voucher_id=reversal_of.id if reversal_of else None,
    )
    voucher.posting_date = posting_date
    voucher.description = description
    session.add(voucher)
    session.flush()
    for index, entry in enumerate(entries, start=1):
        account = (
            get_account_by_role(session, event.org_id, entry.account_role)
            if entry.account_role
            else get_account_by_code(session, event.org_id, entry.account_code or "")
        )
        session.add(
            VoucherLine(
                org_id=event.org_id,
                voucher_id=voucher.id,
                line_number=index,
                account_id=account.id,
                counterparty_id=entry.counterparty_id,
                debit_fen=entry.debit_fen,
                credit_fen=entry.credit_fen,
                memo=entry.memo,
                component_id=entry.component_id,
            )
        )
    session.flush()
    # PostgreSQL protects final voucher lines from every mutation, including
    # INSERT.  Build a complete balanced draft first, then make one final
    # state transition inside the surrounding transaction.
    voucher.status = "posted"
    session.flush()
    return voucher


def account_balance_fen(
    session: Session,
    org_id: uuid.UUID,
    account_role: str,
    *,
    counterparty_id: uuid.UUID | None = None,
) -> int:
    account = get_account_by_role(session, org_id, account_role)
    query = (
        select(
            func.coalesce(func.sum(VoucherLine.debit_fen), 0),
            func.coalesce(func.sum(VoucherLine.credit_fen), 0),
        )
        .join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(Voucher.org_id == org_id, VoucherLine.account_id == account.id)
    )
    if counterparty_id is not None:
        query = query.where(VoucherLine.counterparty_id == counterparty_id)
    debit, credit = session.execute(query).one()
    return int(debit) - int(credit)
