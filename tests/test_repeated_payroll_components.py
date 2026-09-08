from __future__ import annotations

from contextlib import nullcontext
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_payroll_service import preview_and_confirm

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    OpenItem,
    Organization,
    PayrollEventLink,
    PayrollSalaryActualDeductionAllocation,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    Voucher,
)
from ai_accounting.schemas import BankTransactionReference, ReverseEventRequest
from ai_accounting.service import FinanceService


def _salary_source(session: Session, payroll_event_id):
    item = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == payroll_event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert item is not None
    return item


def _salary_component(
    key: str,
    source: OpenItem,
    *,
    gross_fen: int,
    social_fen: int,
    housing_fen: int,
    tax_fen: int,
) -> tuple[dict, int]:
    cash_fen = gross_fen - social_fen - housing_fen - tax_fen
    return (
        {
            "key": key,
            "kind": "salary_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "amount_fen": cash_fen,
            "allocations": [{"open_item_id": source.id, "amount_fen": gross_fen}],
            "withholding_allocations": [
                {
                    "open_item_id": source.id,
                    "employee_social_insurance_items": {"pension": social_fen},
                    "employee_housing_fund_items": {"housing_fund": housing_fen},
                    "individual_income_tax_fen": tax_fen,
                }
            ],
        },
        cash_fen,
    )


def _salary_request(
    organization,
    evidence,
    source: OpenItem,
    *,
    key: str,
    parts: list[tuple[int, int, int, int]],
    account_code: str = "1001",
    bank_transaction_id=None,
) -> RecordEventRequest:
    components_with_cash = [
        _salary_component(
            f"salary-{index}",
            source,
            gross_fen=gross,
            social_fen=social,
            housing_fen=housing,
            tax_fen=tax,
        )
        for index, (gross, social, housing, tax) in enumerate(parts, 1)
    ]
    components = [item[0] for item in components_with_cash]
    cash = [item[1] for item in components_with_cash]
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
            "components": components,
            "funds": [
                {
                    "key": "complete-payment",
                    "account_code": account_code,
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": sum(cash),
                    "allocations": [
                        {"component_key": component["key"], "amount_fen": amount}
                        for component, amount in zip(components, cash, strict=True)
                    ],
                    **(
                        {"bank_transaction_references": [{"id": bank_transaction_id}]}
                        if bank_transaction_id is not None
                        else {}
                    ),
                }
            ],
        }
    )


def _record(session: Session, request: RecordEventRequest, authority=None):
    context = (
        authority.attributed_call(session, tool_name="finance_record_event")
        if authority is not None
        else nullcontext()
    )
    with context:
        return FinanceService(session).record_event(request)


def _formal_counts(session: Session) -> tuple[int, int, int, int, int, int, int]:
    return (
        session.scalar(select(func.count()).select_from(BusinessEvent)),
        session.scalar(select(func.count()).select_from(BusinessEventComponent)),
        session.scalar(select(func.count()).select_from(Voucher)),
        session.scalar(select(func.count()).select_from(OpenItem)),
        session.scalar(select(func.count()).select_from(Settlement)),
        session.scalar(select(func.count()).select_from(PayrollEventLink)),
        session.scalar(
            select(func.count()).select_from(PayrollWithholdingPaymentAllocation)
        ),
    )


def _post_two_salary_components(session, organization, evidence, payroll_event, *, authority=None):
    source = _salary_source(session, payroll_event.id)
    request = _salary_request(
        organization,
        evidence,
        source,
        key="two-salary-components",
        parts=[
            (500_000, 40_000, 35_000, 0),
            (500_000, 40_000, 35_000, 10_500),
        ],
        account_code="1002" if authority is not None else "1001",
    )
    if authority is not None:
        bank = import_test_bank_transaction(
            session,
            organization,
            amount_fen=-839_500,
            booking_date=date(2026, 3, 5),
            key="two-salary-components",
        )
        request = request.model_copy(
            update={
                "funds": [
                    request.funds[0].model_copy(
                        update={
                            "bank_transaction_references": [
                                BankTransactionReference(id=bank.id)
                            ]
                        }
                    )
                ]
            }
        )
    result = _record(session, request, authority)
    assert result.status == "posted", result.errors
    return source, result


def _post_two_statutory_components(
    session,
    organization,
    evidence,
    salary_event_id,
    *,
    authority=None,
    amount_each: int = 20_000,
    key: str = "two-statutory-components",
    expected_posted: bool = True,
):
    source = session.scalar(
        select(OpenItem)
        .join(BusinessEventComponent, BusinessEventComponent.id == OpenItem.source_component_id)
        .where(
            OpenItem.source_event_id == salary_event_id,
            OpenItem.payable_category == "withheld_employee_social",
            BusinessEventComponent.key == "salary-1",
        )
    )
    assert source is not None and source.original_amount_fen == 40_000
    bank = (
        import_test_bank_transaction(
            session,
            organization,
            amount_fen=-(amount_each * 2),
            booking_date=date(2026, 3, 5),
            key=key,
        )
        if authority is not None
        else None
    )
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": key,
                    "kind": "payable_settlement",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "counterparty": {"id": source.counterparty_id},
                    "allocations": [
                        {"open_item_id": source.id, "amount_fen": amount_each}
                    ],
                }
                for key in ("statutory-1", "statutory-2")
            ],
            "funds": [
                {
                    "key": "complete-statutory-payment",
                    "account_code": "1002" if authority is not None else "1001",
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": amount_each * 2,
                    "allocations": [
                        {"component_key": component_key, "amount_fen": amount_each}
                        for component_key in ("statutory-1", "statutory-2")
                    ],
                    **(
                        {"bank_transaction_references": [{"id": bank.id}]}
                        if bank is not None
                        else {}
                    ),
                }
            ],
        }
    )
    result = _record(session, request, authority)
    if not expected_posted:
        return source, result
    assert result.status == "posted", result.errors
    links = list(
        session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.event_id == result.event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
        )
    )
    assert len(links) == 2
    assert {link.source_open_item_id for link in links} == {source.id}
    assert {link.source_payment_event_id for link in links} == {salary_event_id}
    assert len({link.component_id for link in links}) == 2
    return source, result


def _reverse(session, organization, event_id, key, authority=None):
    context = (
        authority.attributed_call(session, tool_name="finance_reverse_event")
        if authority is not None
        else nullcontext()
    )
    with context:
        return FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=event_id,
                idempotency_key=key,
                reason="验证同一事件内重复组件冲正",
                posting_date=date(2026, 3, 5),
            )
        )


def test_repeated_salary_and_statutory_components_post_and_reverse_as_one_plan(
    session: Session, organization
) -> None:
    _, payroll = preview_and_confirm(session, organization)
    payroll_event = session.get(BusinessEvent, payroll.event_id)
    evidence = payroll_event.evidence[0]
    salary_source, salary = _post_two_salary_components(
        session, organization, evidence, payroll_event
    )

    salary_components = list(
        session.scalars(
            select(BusinessEventComponent)
            .where(
                BusinessEventComponent.event_id == salary.event_id,
                BusinessEventComponent.kind == "salary_settlement",
            )
            .order_by(BusinessEventComponent.key)
        )
    )
    assert [item.key for item in salary_components] == ["salary-1", "salary-2"]
    assert len(
        session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.event_id == salary.event_id,
                PayrollEventLink.link_kind == "salary_payment",
            )
        ).all()
    ) == 2
    assert salary_source.status == "settled"

    before_over_settlement = _formal_counts(session)
    statutory_source, over_settlement = _post_two_statutory_components(
        session,
        organization,
        evidence,
        salary.event_id,
        amount_each=25_000,
        key="two-statutory-components-over-source",
        expected_posted=False,
    )
    assert over_settlement.status == "rejected"
    assert over_settlement.errors == ["SETTLEMENT_EXCEEDS_OPEN_BALANCE"]
    assert _formal_counts(session) == before_over_settlement
    assert statutory_source.status == "open" and statutory_source.settled_amount_fen == 0

    statutory_source, statutory = _post_two_statutory_components(
        session,
        organization,
        evidence,
        salary.event_id,
    )
    assert statutory_source.status == "settled"

    statutory_reversal = _reverse(
        session,
        organization,
        statutory.event_id,
        "reverse-two-statutory-components",
    )
    assert statutory_reversal.status == "posted", statutory_reversal.errors
    assert statutory_source.status == "open"
    salary_reversal = _reverse(
        session,
        organization,
        salary.event_id,
        "reverse-two-salary-components",
    )
    assert salary_reversal.status == "posted", salary_reversal.errors
    assert salary_source.status == "open"
    assert all(
        allocation.reversed
        for allocation in session.scalars(
            select(PayrollWithholdingPaymentAllocation).where(
                PayrollWithholdingPaymentAllocation.payment_event_id == salary.event_id
            )
        )
    )


@pytest.mark.parametrize(
    ("key", "parts", "expected_error"),
    [
        (
            "aggregate-gross-over-balance",
            [(600_000, 40_000, 35_000, 0), (500_000, 40_000, 35_000, 10_500)],
            "allocation exceeds open amount",
        ),
        (
            "aggregate-social-over-entitlement",
            [(500_000, 40_000, 35_000, 0), (500_000, 41_000, 35_000, 10_500)],
            "employee social insurance withholding exceeds the payroll-line entitlement",
        ),
        (
            "aggregate-final-missing-tax",
            [(500_000, 40_000, 35_000, 0), (500_000, 40_000, 35_000, 0)],
            "final salary payment must explicitly account for every payroll-line withholding",
        ),
    ],
)
def test_repeated_salary_components_validate_projected_plan_before_formal_writes(
    session: Session, organization, key, parts, expected_error
) -> None:
    _, payroll = preview_and_confirm(session, organization)
    payroll_event = session.get(BusinessEvent, payroll.event_id)
    source = _salary_source(session, payroll.event_id)
    before = _formal_counts(session)
    result = _record(
        session,
        _salary_request(
            organization,
            payroll_event.evidence[0],
            source,
            key=key,
            parts=parts,
        ),
    )
    assert result.status == "rejected"
    assert len(result.errors) == 1 and expected_error in result.errors[0]
    assert _formal_counts(session) == before
    assert source.status == "open" and source.settled_amount_fen == 0


@pytest.mark.postgres
def test_postgres_repeated_salary_components_commit_with_real_attribution_and_bank_import() -> (
    None
):
    with authenticated_business_database("repeated_payroll") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            assert organization is not None and evidence is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                authority=authority,
                evidence_id=evidence_id,
                booking_date=date(2026, 3, 5),
            )
            organization, _, _, evidence, payroll_event = confirmed_payroll(
                session,
                org_id,
                evidence_id,
                authority,
                key="repeated-payroll",
            )
            session.commit()
            source, result = _post_two_salary_components(
                session,
                organization,
                evidence,
                payroll_event,
                authority=authority,
            )
            session.commit()
            assert source.status == "settled"
            assert len(
                session.scalars(
                    select(BusinessEventComponent).where(
                        BusinessEventComponent.event_id == result.event_id,
                        BusinessEventComponent.kind == "salary_settlement",
                    )
                ).all()
            ) == 2
            statutory_source, statutory = _post_two_statutory_components(
                session,
                organization,
                evidence,
                result.event_id,
                authority=authority,
            )
            session.commit()
            assert statutory_source.status == "settled"
            statutory_reversal = _reverse(
                session,
                organization,
                statutory.event_id,
                "postgres-reverse-two-statutory-components",
                authority,
            )
            assert statutory_reversal.status == "posted", statutory_reversal.errors
            session.commit()
            assert statutory_source.status == "open"
            salary_reversal = _reverse(
                session,
                organization,
                result.event_id,
                "postgres-reverse-two-salary-components",
                authority,
            )
            assert salary_reversal.status == "posted", salary_reversal.errors
            session.commit()
            assert source.status == "open"
            assert len(
                session.scalars(
                    select(PayrollEventLink).where(
                        PayrollEventLink.event_id == result.event_id,
                        PayrollEventLink.link_kind == "salary_payment",
                    )
                ).all()
            ) == 2


@pytest.mark.postgres
def test_repeated_salary_actual_deductions_belong_to_each_component():
    with authenticated_business_database("repeated_salary_deductions") as (
        engine, org_id, evidence_id, authority
    ):
        with Session(engine) as session:
            organization, _, _, evidence, payroll_event = confirmed_payroll(
                session, org_id, evidence_id, authority, key="actual-deduction-payroll"
            )
            session.commit()
            source = _salary_source(session, payroll_event.id)
            payload = _salary_request(
                organization, evidence, source, key="repeated-actual-deductions",
                parts=[(500_000, 40_000, 35_000, 0), (500_000, 40_000, 35_000, 10_500)],
            ).model_dump(mode="json")
            for component, fund_allocation, deduction in zip(
                payload["components"], payload["funds"][0]["allocations"], (100, 200), strict=True
            ):
                component["actual_deduction_allocations"] = [
                    {"open_item_id": str(source.id), "amount_fen": deduction}
                ]
                component["amount_fen"] -= deduction
                fund_allocation["amount_fen"] -= deduction
            payload["funds"][0]["amount_fen"] -= 300
            result = _record(session, RecordEventRequest.model_validate(payload), authority)
            assert result.status == "posted", result
            session.commit()
            assert sorted(session.scalars(
                select(PayrollSalaryActualDeductionAllocation.amount_fen).where(
                    PayrollSalaryActualDeductionAllocation.payment_event_id == result.event_id
                )
            )) == [100, 200]
            reversal = _reverse(
                session, organization, result.event_id, "reverse-actual-deductions", authority
            )
            assert reversal.status == "posted", reversal
            session.commit()
            assert source.status == "open"
