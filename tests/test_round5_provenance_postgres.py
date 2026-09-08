"""PostgreSQL commit-boundary regression coverage for R5 provenance rules.

These are deliberately not unit mocks: each attack starts from a canonical
service-created source, bypasses the service only for the malicious draft
transition, and proves that PostgreSQL rejects it at ``COMMIT``.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

import pytest
from _postgres_helpers import authenticated_business_database
from conftest import prepare_authenticated_bank_account
from sqlalchemy import literal, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from test_payroll_service import (
    add_bank_row,
    payment_request,
    payroll_parameters,
    register_payroll_facts,
)
from test_round4_event_integrity_postgres import (
    _post_expense_event,
    _salary_payment_with_unsettled_statutory_sources,
)

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.ledger import ComponentPostingPlan, Entry, commit_posting_plan
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    BusinessEventComponent,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    Voucher,
    event_evidence,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture
def postgres_database():
    with authenticated_business_database("round5_provenance", name="R5 来源完整性") as database:
        yield database


def _stage_exact_inverse_reversal(
    session: Session,
    original: BusinessEvent,
    *,
    key: str,
    execution_attribution_id: object,
    event_type: str = "reversal",
    source_component_id: object | None = None,
    inherit_evidence: bool = False,
    effects: list | None = None,
) -> BusinessEvent:
    """Build an otherwise canonical draft reversal without calling the service."""

    original_voucher = session.scalar(select(Voucher).where(Voucher.event_id == original.id))
    assert original_voucher is not None
    reversal = BusinessEvent(
        org_id=original.org_id,
        idempotency_key=key,
        event_type=event_type,
        status="draft",
        description="R5 直接SQL冲正攻击",
        facts={
            "original_event_id": str(original.id),
            "source_event_id": str(original.id),
            "source_component_id": (
                str(source_component_id) if source_component_id is not None else None
            ),
            "reversal": True,
        },
        business_date=date(2026, 3, 6),
        posting_date=date(2026, 3, 6),
        rule_trace=[],
        rule_version=original.rule_version,
        execution_attribution_id=execution_attribution_id,
    )
    session.add(reversal)
    session.flush()
    if inherit_evidence:
        session.execute(
            event_evidence.insert().from_select(
                ["org_id", "event_id", "evidence_id", "relation_kind"],
                select(
                    event_evidence.c.org_id,
                    literal(reversal.id),
                    event_evidence.c.evidence_id,
                    literal("inherited"),
                ).where(event_evidence.c.event_id == original.id),
            )
        )
    entries = [
        Entry(
            account_code=line.account.code,
            debit_fen=line.credit_fen,
            credit_fen=line.debit_fen,
            counterparty_id=line.counterparty_id,
        )
        for line in original_voucher.lines
    ]
    commit_posting_plan(
        session,
        event=reversal,
        posting_date=date(2026, 3, 6),
        description=reversal.description,
        reversal_of=original_voucher,
        components=[
            ComponentPostingPlan(
                key="reversal",
                kind=event_type,
                facts=dict(reversal.facts),
                entries=entries,
                effects=effects or [],
            )
        ],
    )
    return reversal


def _post_expense_with_supporting_evidence(
    session: Session,
    organization: Organization,
    evidence_id: object,
    authority,
    *,
    key: str,
) -> BusinessEvent:
    event = _post_expense_event(
        session,
        organization,
        authority,
        evidence_id,
        key=f"r5-{key}-expense",
    )
    supporting = session.scalars(
        select(event_evidence.c.evidence_id).where(
            event_evidence.c.org_id == organization.id,
            event_evidence.c.event_id == event.id,
            event_evidence.c.relation_kind == "supporting",
        )
    ).all()
    assert supporting == [evidence_id]
    return event


def test_r5_005_postgres_rejects_final_normal_reversal_without_inherited_evidence(
    postgres_database,
) -> None:
    """A direct draft->posted normal reversal cannot drop the source evidence set."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        original = _post_expense_with_supporting_evidence(
            session,
            organization,
            evidence_id,
            authority,
            key="missing-inherited",
        )
        identifiers = {"org_id": organization.id, "original_event_id": original.id}
        session.commit()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["original_event_id"])
        assert original is not None
        with authority.attributed_call(
            session, tool_name="finance_test_missing_reversal_evidence"
        ) as attribution:
            reversal = _stage_exact_inverse_reversal(
                session,
                original,
                key="r5-missing-inherited",
                execution_attribution_id=attribution.id,
            )
            # This is the bypass: all voucher/state facts are otherwise canonical,
            # but the draft gets no ``inherited`` event_evidence edge at all.
            original.status = "reversed"
            original.reversed_by_event_id = reversal.id
            reversal.status = "posted"
            with pytest.raises(DBAPIError):
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["original_event_id"])
        assert original is not None and original.status == "posted"
        assert original.reversed_by_event_id is None


def _stage_salary_reversal_then_delete_draft_pel(
    session: Session,
    *,
    org_id: object,
    original: BusinessEvent,
    execution_attribution_id: object,
) -> BusinessEvent:
    """Reproduce R5-005's deleted-in-draft PEL attack with every other effect intact."""

    original_link = session.scalar(
        select(PayrollEventLink).where(
            PayrollEventLink.org_id == org_id,
            PayrollEventLink.event_id == original.id,
            PayrollEventLink.link_kind == "salary_payment",
        )
    )
    assert original_link is not None

    def add_then_delete_link(target_session, target_event, component):
        draft_link = PayrollEventLink(
            org_id=org_id,
            event_id=target_event.id,
            payroll_batch_id=original_link.payroll_batch_id,
            source_payment_event_id=original.id,
            source_open_item_id=original_link.source_open_item_id,
            link_kind="reversal",
            component_id=component.id,
        )
        target_session.add(draft_link)
        target_session.flush()
        target_session.delete(draft_link)

    reversal = _stage_exact_inverse_reversal(
        session,
        original,
        key="r5-delete-salary-reversal-link",
        execution_attribution_id=execution_attribution_id,
        source_component_id=original_link.component_id,
        inherit_evidence=True,
        effects=[add_then_delete_link],
    )

    for item in session.scalars(
        select(OpenItem).where(OpenItem.org_id == org_id, OpenItem.source_event_id == original.id)
    ):
        item.status = "reversed"
    settlements = session.scalars(
        select(Settlement)
        .where(
            Settlement.org_id == org_id,
            Settlement.payment_event_id == original.id,
            Settlement.reversed.is_(False),
        )
        .with_for_update()
    ).all()
    assert settlements
    for settlement in settlements:
        settlement.open_item.settled_amount_fen -= settlement.amount_fen
        settlement.open_item.status = (
            "open" if settlement.open_item.settled_amount_fen == 0 else "partial"
        )
        settlement.reversed = True
        # R5-001 makes this an organization-bound, auditable reversal relation.
        settlement.reversed_by_event_id = reversal.id

    matches = session.scalars(
        select(BankTransactionMatch)
        .where(
            BankTransactionMatch.org_id == org_id,
            BankTransactionMatch.event_id == original.id,
            BankTransactionMatch.invalidated_by_event_id.is_(None),
        )
        .with_for_update()
    ).all()
    for match in matches:
        match.invalidated_by_event_id = reversal.id
        match.invalidated_at = datetime.now().astimezone()
        bank = session.get(BankTransaction, match.bank_transaction_id)
        assert bank is not None
        bank.matched_event_id = None
    for allocation in session.scalars(
        select(PayrollWithholdingPaymentAllocation).where(
            PayrollWithholdingPaymentAllocation.org_id == org_id,
            PayrollWithholdingPaymentAllocation.payment_event_id == original.id,
            PayrollWithholdingPaymentAllocation.reversed.is_(False),
        )
    ):
        allocation.reversed = True
        allocation.reversed_by_event_id = reversal.id

    original.status = "reversed"
    original.reversed_by_event_id = reversal.id
    reversal.status = "posted"
    return reversal


def test_r5_005_postgres_rejects_salary_reversal_after_draft_pel_delete(
    postgres_database,
) -> None:
    """A final salary-payment reversal must retain its exact canonical PEL."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        identifiers = _salary_payment_with_unsettled_statutory_sources(
            session, org_id, evidence_id, authority, key="delete-reversal-pel"
        )
        source_event_id = session.scalar(
            select(OpenItem.source_event_id).where(
                OpenItem.org_id == identifiers["org_id"],
                OpenItem.id == identifiers["withheld_social_item_id"],
            )
        )
        assert source_event_id is not None
        identifiers["salary_event_id"] = source_event_id
        session.commit()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["salary_event_id"])
        assert original is not None and original.event_type == "composite"
        assert (
            session.scalar(
                select(BusinessEventComponent.id).where(
                    BusinessEventComponent.event_id == original.id,
                    BusinessEventComponent.kind == "salary_settlement",
                )
            )
            is not None
        )
        with authority.attributed_call(
            session, tool_name="finance_test_delete_reversal_link"
        ) as attribution:
            _stage_salary_reversal_then_delete_draft_pel(
                session,
                org_id=identifiers["org_id"],
                original=original,
                execution_attribution_id=attribution.id,
            )
            with pytest.raises(DBAPIError):
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["salary_event_id"])
        assert original is not None and original.status == "posted"
        assert original.reversed_by_event_id is None


def test_r5_005_event_query_projects_relational_reversal_evidence_chain(
    postgres_database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP event read model exposes supporting/inherited roles from relation tables."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        original = _post_expense_with_supporting_evidence(
            session,
            organization,
            evidence_id,
            authority,
            key="event-query-chain",
        )
        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=original.id,
                    idempotency_key="r5-event-query-chain-reversal",
                    reason="R5 查询规范冲正链",
                    posting_date=date(2026, 3, 6),
                )
            )
        assert reversal.status == "posted", reversal.errors
        identifiers = {
            "org_id": organization.id,
            "original_event_id": original.id,
            "reversal_event_id": reversal.event_id,
        }
        session.commit()

    from ai_accounting import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        sessionmaker(bind=postgres_engine, expire_on_commit=False, autoflush=True),
    )
    response = mcp_server.finance_get_event(
        str(identifiers["org_id"]), str(identifiers["reversal_event_id"])
    )
    assert response["status"] == "ok"
    chain = response["canonical_reversal_chain"]
    assert chain["root_event_id"] == str(identifiers["original_event_id"])
    assert chain["terminal_event_id"] == str(identifiers["reversal_event_id"])
    by_event_id = {item["id"]: item for item in chain["events"]}
    assert by_event_id[str(identifiers["original_event_id"])]["reversed_by_event_id"] == str(
        identifiers["reversal_event_id"]
    )
    assert by_event_id[str(identifiers["reversal_event_id"])]["reversal_of_event_id"] == str(
        identifiers["original_event_id"]
    )
    evidence_roles = {(item["event_id"], item["relation_kind"]) for item in chain["event_evidence"]}
    assert (str(identifiers["original_event_id"]), "supporting") in evidence_roles
    assert (str(identifiers["reversal_event_id"]), "inherited") in evidence_roles


def _preview_regular(
    session: Session,
    *,
    org_id: object,
    employee_id: object,
    payroll_period: str,
    evidence_id: object,
    authority,
    key: str,
) -> object:
    day = date.fromisoformat(f"{payroll_period}-05")
    with authority.attributed_call(session, tool_name="finance_preview_payroll"):
        result = FinanceService(session).preview_payroll(
            PreviewPayrollRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": key,
                    "batch_kind": "regular",
                    "payroll_period": payroll_period,
                    "posting_date": day.isoformat(),
                    "payment_date": day.isoformat(),
                    "evidence_references": [evidence_id],
                    "employee_items": [
                        {
                            "employee_id": employee_id,
                            "tax_reported_salary_fen": 1_000_000,
                            "special_additional_deduction_fen": 0,
                            "other_legal_deduction_fen": 0,
                        }
                    ],
                }
            )
        )
    assert not result.missing_information, result.missing_information
    assert result.status == "calculated", result.errors
    return result


def _preview_separate_bonus(
    session: Session,
    *,
    org_id: object,
    employee_id: object,
    evidence_id: object,
    authority,
    key: str,
    payment_date: date = date(2026, 3, 5),
) -> object:
    with authority.attributed_call(session, tool_name="finance_preview_payroll"):
        result = FinanceService(session).preview_payroll(
            PreviewPayrollRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": key,
                    "batch_kind": "annual_bonus",
                    "payroll_period": "2026-03",
                    "posting_date": payment_date.isoformat(),
                    "payment_date": payment_date.isoformat(),
                    "tax_method": "separate",
                    "evidence_references": [evidence_id],
                    "employee_items": [{"employee_id": employee_id, "annual_bonus_fen": 100_000}],
                }
            )
        )
    assert result.status == "calculated", result.errors
    return result


def _confirm(session: Session, *, org_id: object, preview: object, authority, key: str) -> object:
    with authority.attributed_call(session, tool_name="finance_confirm_payroll"):
        result = FinanceService(session).confirm_payroll(
            ConfirmPayrollRequest(
                org_id=org_id,
                batch_id=preview.batch_id,
                calculation_hash=preview.calculation_hash,
                idempotency_key=key,
            )
        )
    assert result.status == "posted", result.errors
    return result


def _register_payroll_facts(session, organization, evidence_id, authority):
    prepare_authenticated_bank_account(
        session,
        organization,
        authority=authority,
        evidence_id=evidence_id,
    )
    with authority.attributed_call(session, tool_name="finance_register_payroll_facts"):
        return register_payroll_facts(session, organization)


def _post_full_salary_payment(
    session: Session,
    organization: object,
    *,
    batch_id: object,
    accrual_event_id: object,
    evidence_id: object,
    authority,
    key: str,
) -> tuple[object, OpenItem]:
    """Pay one batch's salary and return its resulting individual-tax source item."""

    batch = session.get(PayrollBatch, batch_id)
    line = session.scalar(
        select(PayrollLine).where(
            PayrollLine.org_id == organization.id, PayrollLine.payroll_batch_id == batch_id
        )
    )
    salary = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == accrual_event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert batch is not None and line is not None and salary is not None
    payment_date = batch.payment_date or batch.posting_date
    bank = add_bank_row(
        session,
        organization,
        -line.net_salary_fen,
        f"{key}-bank",
        booking_date=payment_date,
    )
    request = payment_request(
        organization,
        event_type="salary_payment",
        amount_fen=line.net_salary_fen,
        allocations=[{"open_item_id": salary.id, "amount_fen": salary.original_amount_fen}],
        salary_withholdings=[
            {
                "open_item_id": salary.id,
                "employee_social_insurance_items": line.employee_social_insurance_items,
                "employee_housing_fund_items": line.employee_housing_fund_items,
                "individual_income_tax_fen": line.individual_income_tax_fen,
            }
        ],
        bank=bank,
        key=key,
    )
    request = request.model_copy(
        update={
            "evidence_references": [evidence_id],
            "posting_date": batch.posting_date,
            "components": [
                request.components[0].model_copy(
                    update={
                        "business_date": payment_date,
                        "payment_date": payment_date,
                    }
                )
            ],
            "funds": [request.funds[0].model_copy(update={"payment_date": payment_date})],
        }
    )
    with authority.attributed_call(session, tool_name="finance_record_event"):
        result = FinanceService(session).record_event(request)
    assert result.status == "posted", result.errors
    tax_item = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == result.event_id,
            OpenItem.payable_category == "individual_income_tax",
        )
    )
    assert tax_item is not None and tax_item.original_amount_fen > 0
    return result, tax_item


def _post_regular_tax_source(
    session: Session,
    organization: object,
    *,
    employee_id: object,
    payroll_period: str,
    evidence_id: object,
    authority,
    key: str,
) -> tuple[object, OpenItem]:
    preview = _preview_regular(
        session,
        org_id=organization.id,
        employee_id=employee_id,
        payroll_period=payroll_period,
        evidence_id=evidence_id,
        authority=authority,
        key=f"{key}-preview",
    )
    confirmation = _confirm(
        session,
        org_id=organization.id,
        preview=preview,
        authority=authority,
        key=f"{key}-confirm",
    )
    _payment, tax_item = _post_full_salary_payment(
        session,
        organization,
        batch_id=preview.batch_id,
        accrual_event_id=confirmation.event_id,
        evidence_id=evidence_id,
        authority=authority,
        key=f"{key}-salary",
    )
    return preview, tax_item


def _multi_statutory_request(
    organization,
    *,
    items: list[OpenItem],
    bank: BankTransaction,
    evidence_id: object,
    key: str,
    payment_date: date,
) -> RecordEventRequest:
    components = [
        {
            "key": f"statutory-{index}",
            "kind": "payable_settlement",
            "business_date": payment_date,
            "payment_date": payment_date,
            "counterparty": {"id": item.counterparty_id},
            "allocations": [{"open_item_id": item.id, "amount_fen": item.original_amount_fen}],
        }
        for index, item in enumerate(items, 1)
    ]
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": payment_date,
            "evidence_references": [evidence_id],
            "components": components,
            "funds": [
                {
                    "key": "bank",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": payment_date,
                    "amount_fen": sum(item.original_amount_fen for item in items),
                    "allocations": [
                        {
                            "component_key": component["key"],
                            "amount_fen": item.original_amount_fen,
                        }
                        for component, item in zip(components, items, strict=True)
                    ],
                    "bank_transaction_references": [{"id": bank.id}],
                }
            ],
        }
    )


def test_r5_007_compatible_multi_batch_tax_payment_keeps_per_source_provenance(
    postgres_database,
) -> None:
    """Compatible regular and separate-bonus batches settle through one tax payment.

    The two formal batches share organization, tax agency, policy version,
    contribution/tax period and CNY, while their normalized PEL edges must
    retain their own batch, salary-payment event and open item.
    """

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        employee_id = _register_payroll_facts(session, organization, evidence_id, authority)
        regular_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            evidence_id=evidence_id,
            authority=authority,
            key="r5-multi-regular-preview",
        )
        regular = _confirm(
            session,
            org_id=organization.id,
            preview=regular_preview,
            authority=authority,
            key="r5-multi-regular-confirm",
        )
        bonus_preview = _preview_separate_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            evidence_id=evidence_id,
            authority=authority,
            key="r5-multi-bonus-preview",
        )
        bonus = _confirm(
            session,
            org_id=organization.id,
            preview=bonus_preview,
            authority=authority,
            key="r5-multi-bonus-confirm",
        )
        _regular_salary, regular_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=regular_preview.batch_id,
            accrual_event_id=regular.event_id,
            evidence_id=evidence_id,
            authority=authority,
            key="r5-multi-regular-salary",
        )
        _bonus_salary, bonus_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=bonus_preview.batch_id,
            accrual_event_id=bonus.event_id,
            evidence_id=evidence_id,
            authority=authority,
            key="r5-multi-bonus-salary",
        )
        amount_fen = regular_tax.original_amount_fen + bonus_tax.original_amount_fen
        request = payment_request(
            organization,
            event_type="individual_income_tax_payment",
            amount_fen=amount_fen,
            allocations=[
                {"open_item_id": regular_tax.id, "amount_fen": regular_tax.original_amount_fen},
                {"open_item_id": bonus_tax.id, "amount_fen": bonus_tax.original_amount_fen},
            ],
            bank=add_bank_row(session, organization, -amount_fen, "r5-multi-tax-bank"),
            key="r5-compatible-multi-batch-tax-payment",
        ).model_copy(update={"evidence_references": [evidence_id]})
        with authority.attributed_call(session, tool_name="finance_record_event"):
            tax_payment = FinanceService(session).record_event(request)
        assert tax_payment.status == "posted", tax_payment.errors
        with authority.attributed_call(session, tool_name="finance_record_event"):
            replay = FinanceService(session).record_event(request)
        assert replay.status == "posted" and replay.event_id == tax_payment.event_id

        edges = session.scalars(
            select(PayrollEventLink)
            .where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == tax_payment.event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
            .order_by(PayrollEventLink.source_open_item_id)
        ).all()
        assert len(edges) == 2
        assert {(edge.payroll_batch_id, edge.source_open_item_id) for edge in edges} == {
            (regular_preview.batch_id, regular_tax.id),
            (bonus_preview.batch_id, bonus_tax.id),
        }
        assert {edge.source_payment_event_id for edge in edges} == {
            regular_tax.source_event_id,
            bonus_tax.source_event_id,
        }

        lifecycle = FinanceService(session).get_payroll_batch(
            organization.id, regular_preview.batch_id
        )["lifecycle"]
        queried = {item["id"]: item for item in lifecycle["payroll_event_links"]}
        for edge in edges:
            relation = queried[str(edge.id)]
            assert relation["payroll_batch_id"] == str(edge.payroll_batch_id)
            assert relation["source_payment_event_id"] == str(edge.source_payment_event_id)
            assert relation["source_open_item_id"] == str(edge.source_open_item_id)
            assert relation["source_open_item"]["payable_category"] == "individual_income_tax"

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=tax_payment.event_id,
                    idempotency_key="r5-compatible-multi-batch-tax-reversal",
                    reason="R5 多批次法定缴款冲正",
                    posting_date=date(2026, 3, 6),
                )
            )
        assert reversal.status == "posted", reversal.errors
        reversal_edges = session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == reversal.event_id,
                PayrollEventLink.link_kind == "reversal",
            )
        ).all()
        assert {
            (edge.payroll_batch_id, edge.source_open_item_id, edge.source_payment_event_id)
            for edge in reversal_edges
        } == {
            (edge.payroll_batch_id, edge.source_open_item_id, tax_payment.event_id)
            for edge in edges
        }
        session.commit()


def test_r5_007_multi_period_tax_payment_preserves_each_source(
    postgres_database,
) -> None:
    """Same-agency IIT sources across periods may settle with exact provenance."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        employee_id = _register_payroll_facts(session, organization, evidence_id, authority)
        september, september_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            evidence_id=evidence_id,
            authority=authority,
            key="r5-period-september",
        )
        prepare_authenticated_bank_account(
            session,
            organization,
            booking_date=date(2026, 4, 5),
            authority=authority,
            evidence_id=evidence_id,
        )
        october, october_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-04",
            evidence_id=evidence_id,
            authority=authority,
            key="r5-period-october",
        )
        amount_fen = september_tax.original_amount_fen + october_tax.original_amount_fen
        bank = add_bank_row(
            session,
            organization,
            -amount_fen,
            "r5-period-incompatible-bank",
            booking_date=date(2026, 4, 5),
        )
        request = payment_request(
            organization,
            event_type="individual_income_tax_payment",
            amount_fen=amount_fen,
            allocations=[
                {"open_item_id": september_tax.id, "amount_fen": september_tax.original_amount_fen},
                {"open_item_id": october_tax.id, "amount_fen": october_tax.original_amount_fen},
            ],
            bank=bank,
            key="r5-period-incompatible-tax-payment",
        )
        request = request.model_copy(
            update={
                "evidence_references": [evidence_id],
                "posting_date": date(2026, 4, 5),
                "components": [
                    request.components[0].model_copy(
                        update={
                            "business_date": date(2026, 4, 5),
                            "payment_date": date(2026, 4, 5),
                        }
                    )
                ],
                "funds": [request.funds[0].model_copy(update={"payment_date": date(2026, 4, 5)})],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            result = FinanceService(session).record_event(request)
        assert result.status == "posted", result.errors
        for item in (september_tax, october_tax):
            assert item.status == "settled"
            assert item.settled_amount_fen == item.original_amount_fen
        links = session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.event_id == result.event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
        ).all()
        assert {(link.source_open_item_id, link.source_payment_event_id) for link in links} == {
            (september_tax.id, september_tax.source_event_id),
            (october_tax.id, october_tax.source_event_id),
        }
        assert september.batch_id != october.batch_id
        session.commit()


def test_r5_007_different_policy_and_agency_sources_post_as_separate_components(
    postgres_database,
) -> None:
    """Different statutory agencies retain independent components in one payment."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        prepare_authenticated_bank_account(
            session,
            organization,
            authority=authority,
            evidence_id=evidence_id,
        )
        service = FinanceService(session)
        with authority.attributed_call(session, tool_name="finance_register_employee"):
            employee = service.register_employee(
                RegisterEmployeeRequest(
                    org_id=organization.id,
                    employee_code="R5-POLICY-001",
                    name="政策边界员工",
                    employment_start_date=date(2026, 3, 1),
                    tax_withholding_start_date=date(2026, 3, 1),
                    status="active",
                )
            )
        employee_id = employee["employee_id"]
        assert employee["status"] == "registered"
        with authority.attributed_call(
            session, tool_name="finance_register_employee_payroll_profile_version"
        ):
            profile = service.register_employee_payroll_profile_version(
                RegisterEmployeePayrollProfileVersionRequest(
                    org_id=organization.id,
                    employee_id=employee_id,
                    effective_from=date(2026, 3, 1),
                    expense_role="payroll_management_expense",
                    social_insurance_base_fen=1_000_000,
                    housing_fund_base_fen=1_000_000,
                    resident_employee=True,
                )
            )
        assert profile["status"] == "registered"
        # Regular payroll selects its tax policy at the payroll-period end,
        # while a separately taxed annual bonus selects at its payment date.
        # A lawful boundary between those dates can therefore freeze distinct
        # statutory agencies into two otherwise compatible payable sources.
        first_parameters = deepcopy(payroll_parameters())
        with authority.attributed_call(
            session, tool_name="finance_register_payroll_policy_version"
        ):
            first_policy = service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest(
                    org_id=organization.id,
                    region="R5 政策边界地区",
                    effective_from=date(2025, 7, 1),
                    effective_to=date(2026, 3, 30),
                    version="r5-policy-agency-v1",
                    source_url=(
                        "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/"
                        "201812/c4182700/content.html"
                    ),
                    parameters=first_parameters,
                )
            )
        assert first_policy["status"] == "registered"
        second_parameters = deepcopy(payroll_parameters())
        second_parameters["payment_targets"]["individual_income_tax"] = {
            "agency_code": "TAX-02",
            "agency_name": "第二税务局",
        }
        with authority.attributed_call(
            session, tool_name="finance_register_payroll_policy_version"
        ):
            second_policy = service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest(
                    org_id=organization.id,
                    region="R5 政策边界地区",
                    effective_from=date(2026, 3, 31),
                    effective_to=date(2026, 6, 30),
                    version="r5-policy-agency-v2",
                    source_url=(
                        "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/"
                        "201812/c4182700/content.html"
                    ),
                    parameters=second_parameters,
                )
            )
        assert second_policy["status"] == "registered"

        regular_preview, regular_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            evidence_id=evidence_id,
            authority=authority,
            key="r5-policy-agency-regular",
        )
        bonus_preview = _preview_separate_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            evidence_id=evidence_id,
            authority=authority,
            key="r5-policy-agency-bonus-preview",
            payment_date=date(2026, 3, 6),
        )
        bonus = _confirm(
            session,
            org_id=organization.id,
            preview=bonus_preview,
            authority=authority,
            key="r5-policy-agency-bonus-confirm",
        )
        _bonus_salary, bonus_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=bonus_preview.batch_id,
            accrual_event_id=bonus.event_id,
            evidence_id=evidence_id,
            authority=authority,
            key="r5-policy-agency-bonus-salary",
        )
        assert regular_preview.batch_id != bonus_preview.batch_id
        amount_fen = regular_tax.original_amount_fen + bonus_tax.original_amount_fen
        bank = add_bank_row(
            session,
            organization,
            -amount_fen,
            "r5-policy-agency-bank",
            booking_date=date(2026, 3, 6),
        )
        request = _multi_statutory_request(
            organization,
            items=[regular_tax, bonus_tax],
            bank=bank,
            evidence_id=evidence_id,
            key="r5-policy-agency-composite-payment",
            payment_date=date(2026, 3, 6),
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            result = FinanceService(session).record_event(request)
        assert result.status == "posted", result.errors
        for item in (regular_tax, bonus_tax):
            assert item.status == "settled"
            assert item.settled_amount_fen == item.original_amount_fen
        components = session.scalars(
            select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == result.event_id,
                BusinessEventComponent.kind == "payable_settlement",
            )
        ).all()
        assert len(components) == 2
        session.commit()


def test_r5_007_different_statutory_categories_post_as_separate_components(
    postgres_database,
) -> None:
    """IIT and social liabilities keep separate provenance under one cash movement."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        employee_id = _register_payroll_facts(session, organization, evidence_id, authority)
        preview, tax_item = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            evidence_id=evidence_id,
            authority=authority,
            key="r5-category-local",
        )
        batch = session.get(PayrollBatch, preview.batch_id)
        assert batch is not None and batch.business_event_id is not None
        social_item = session.scalar(
            select(OpenItem).where(
                OpenItem.org_id == organization.id,
                OpenItem.source_event_id == batch.business_event_id,
                OpenItem.payable_category == "employer_social",
            )
        )
        assert social_item is not None

        category_amount = tax_item.original_amount_fen + social_item.original_amount_fen
        category_request = _multi_statutory_request(
            organization,
            items=[tax_item, social_item],
            bank=add_bank_row(
                session, organization, -category_amount, "r5-category-composite-bank"
            ),
            evidence_id=evidence_id,
            key="r5-category-composite-payment",
            payment_date=date(2026, 3, 5),
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            category_rejection = FinanceService(session).record_event(category_request)
        assert category_rejection.status == "posted", category_rejection.errors
        assert tax_item.status == social_item.status == "settled"
        links = session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.event_id == category_rejection.event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
        ).all()
        assert {link.source_open_item_id for link in links} == {tax_item.id, social_item.id}
        session.commit()
