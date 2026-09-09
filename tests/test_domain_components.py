from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from ai_accounting.borrowing_schemas import PreviewBorrowingInterestRequest
from ai_accounting.borrowing_service import BorrowingService
from ai_accounting.domain_action_schemas import (
    BorrowingDrawdownFacts,
    BorrowingInterestAccrualFacts,
    FixedAssetAcquisitionFacts,
    IntangibleAssetAcquisitionFacts,
)
from ai_accounting.domain_components import compile_borrowing_payment, compile_domain_action
from ai_accounting.ledger import ComponentPostingPlan, Entry, commit_posting_plan
from ai_accounting.models import (
    Borrowing,
    BorrowingPayment,
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    FixedAsset,
    IntangibleAsset,
    OpenItem,
    Voucher,
    VoucherLine,
)


def evidence(session, org):
    row = Evidence(
        org_id=org.id,
        sha256="d" * 64,
        original_name="domain.pdf",
        media_type="application/pdf",
        source="test",
        size_bytes=1,
        storage_path="test/domain",
    )
    session.add(row)
    session.flush()
    return row


def event(org, key, day):
    return BusinessEvent(
        id=uuid.uuid4(),
        org_id=org.id,
        idempotency_key=key,
        event_type="composite",
        status="draft",
        facts={},
        business_date=day,
        posting_date=day,
        rule_trace=[],
    )


def compile_action(session, org, proof, key, kind, facts, day):
    return compile_domain_action(
        session,
        org_id=org.id,
        key=key,
        kind=kind,
        facts=facts,
        posting_date=day,
        business_date=day,
        payment_date=None,
        evidence_references=[proof.id],
    )


def test_asset_plans_share_parent_and_only_create_subledgers_at_commit(session, organization):
    proof = evidence(session, organization)
    day = date(2026, 1, 2)
    fixed = compile_action(
        session,
        organization,
        proof,
        "equipment",
        "fixed_asset_acquisition",
        FixedAssetAcquisitionFacts.model_validate(
            {
                "category": "production_equipment",
                "expected_use_over_one_year": True,
                "cost_fen": 100000,
                "cost_components": {
                    "purchase_price_fen": 100000,
                    "noncreditable_tax_fen": 0,
                    "transport_and_handling_fen": 0,
                    "installation_and_direct_cost_fen": 0,
                },
                "settlement_method": "payable",
                "claims_creditable_input_vat": False,
            }
        ),
        day,
    )
    intangible = compile_action(
        session,
        organization,
        proof,
        "license",
        "intangible_asset_acquisition",
        IntangibleAssetAcquisitionFacts.model_validate(
            {
                "category": "software",
                "available_for_use_date": day,
                "cost_fen": 12000,
                "cost_components": {
                    "purchase_price_fen": 12000,
                    "noncreditable_tax_fen": 0,
                    "directly_attributable_cost_fen": 0,
                },
                "settlement_method": "payable",
                "benefit_area": "management",
                "life_basis": "legal_or_contractual",
                "useful_life_months": 12,
                "is_available_for_use": True,
                "claims_creditable_input_vat": False,
            }
        ),
        day,
    )
    assert session.scalar(select(FixedAsset)) is None
    assert session.scalar(select(IntangibleAsset)) is None
    assert session.scalar(select(BusinessEvent)) is None
    parent = event(organization, "assets-together", day)
    voucher = commit_posting_plan(
        session,
        event=parent,
        components=[fixed, intangible],
        posting_date=day,
        description="购置设备和软件",
    )
    components = list(session.scalars(select(BusinessEventComponent)))
    component_ids = {c.id for c in components}
    assert {c.key for c in components} == {"equipment", "license"}
    assert {row.source_component_id for row in session.scalars(select(OpenItem))} == component_ids
    assert session.scalar(select(FixedAsset)).component_id in component_ids
    assert session.scalar(select(IntangibleAsset)).component_id in component_ids
    lines = list(session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher.id)))
    assert sum(row.debit_fen for row in lines) == sum(row.credit_fen for row in lines) == 112000
    assert {row.component_id for row in lines} == component_ids
    assert len(list(session.scalars(select(Voucher)))) == 1
    assert parent.status == "posted"


@pytest.mark.parametrize("same_event_accrual", [False, True])
def test_borrowing_interest_and_principal_compile_in_either_order(
    session, organization, same_event_accrual
):
    proof = evidence(session, organization)
    draw_day, due_day = date(2026, 1, 1), date(2026, 1, 31)
    facts = BorrowingDrawdownFacts.model_validate(
        {
            "lender": {"name": "银行"},
            "lender_is_licensed_financial_institution": True,
            "currency": "CNY",
            "principal_fen": 1000000,
            "due_date": due_day,
            "annual_rate_percent": "3.65",
            "day_count_basis": "actual_365",
            "capitalization_applicable": False,
            "term_facts": {
                "single_drawdown": True,
                "fixed_rate": True,
                "simple_interest": True,
                "bullet_principal_at_maturity": True,
                "allows_prepayment": False,
                "allows_extension": False,
                "has_penalty_interest": False,
                "has_financing_fees": False,
            },
        }
    )
    draw = compile_action(
        session, organization, proof, "loan", "borrowing_drawdown", facts, draw_day
    )
    assert session.scalar(select(Borrowing)) is None
    draw_parent = event(organization, "loan-draw", draw_day)
    funding = ComponentPostingPlan(
        key="cash",
        kind="money_movement",
        facts={},
        entries=[Entry(account_code="1002", debit_fen=1000000)],
    )
    commit_posting_plan(
        session,
        event=draw_parent,
        components=[draw, funding],
        posting_date=draw_day,
        description="借款到账",
    )
    borrowing = session.scalar(select(Borrowing))
    preview = BorrowingService(session).preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=draw_day,
            period_end=due_day,
        )
    )
    accrual = compile_action(
        session,
        organization,
        proof,
        "interest",
        "borrowing_interest_accrual",
        BorrowingInterestAccrualFacts(
            borrowing_id=borrowing.id,
            period_start=draw_day,
            period_end=due_day,
            calculation_hash=preview.calculation_hash,
        ),
        due_day,
    )
    accrual_parent = event(organization, "loan-accrual", due_day)
    if same_event_accrual:
        from ai_accounting.component_schemas import RecordEventRequest
        from ai_accounting.component_service import ComponentService

        interest_amount = sum(line.debit_fen for line in accrual.entries)
        common_facts = {
            "business_date": due_day,
            "payment_date": due_day,
            "borrowing_id": borrowing.id,
        }
        payload = RecordEventRequest(
            org_id=organization.id,
            idempotency_key="accrue-pay-and-repay",
            posting_date=due_day,
            evidence_references=[proof.id],
            components=[
                {
                    "key": "principal",
                    "kind": "borrowing_principal_repayment",
                    "amount_fen": borrowing.principal_fen,
                    **common_facts,
                },
                {
                    "key": "interest-payment",
                    "kind": "borrowing_interest_payment",
                    "accrual_component_key": "accrual",
                    "amount_fen": interest_amount,
                    **common_facts,
                },
                {
                    "key": "accrual",
                    "kind": "borrowing_interest_accrual",
                    "business_date": due_day,
                    "facts": {
                        "borrowing_id": borrowing.id,
                        "period_start": draw_day,
                        "period_end": due_day,
                        "calculation_hash": preview.calculation_hash,
                    },
                },
            ],
            funds=[
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": due_day,
                    "amount_fen": borrowing.principal_fen + interest_amount,
                    "allocations": [
                        {"component_key": "principal", "amount_fen": borrowing.principal_fen},
                        {"component_key": "interest-payment", "amount_fen": interest_amount},
                    ],
                }
            ],
        )
        result = ComponentService(session).record(payload)
        assert result.status == "posted", result
        payments = list(session.scalars(select(BorrowingPayment)))
        assert {p.event_id for p in payments} == {result.event_id}
        assert {p.payment_kind for p in payments} == {"interest", "principal"}
        assert len(list(session.scalars(select(Voucher)))) == 2
        assert ComponentService(session).record(payload).event_id == result.event_id
        return
    commit_posting_plan(
        session,
        event=accrual_parent,
        components=[accrual],
        posting_date=due_day,
        description="计提利息",
    )
    common = dict(
        session=session,
        org_id=organization.id,
        borrowing_id=borrowing.id,
        posting_date=due_day,
        payment_date=due_day,
    )
    with pytest.raises(ValueError, match="REQUIRES_INTEREST_SETTLEMENT"):
        compile_borrowing_payment(
            **common, key="principal", kind="borrowing_principal_repayment", amount_fen=1000000
        )
    principal = compile_borrowing_payment(
        **common,
        key="principal",
        kind="borrowing_principal_repayment",
        amount_fen=1000000,
        pending_interest_accrual_event_ids={accrual_parent.id},
    )
    interest_amount = sum(line.debit_fen for line in accrual.entries)
    interest = compile_borrowing_payment(
        **common,
        key="interest-payment",
        kind="borrowing_interest_payment",
        accrual_event_id=accrual_parent.id,
        amount_fen=interest_amount,
    )
    assert session.scalar(select(BorrowingPayment)) is None
    payment_parent = event(organization, "loan-repaid", due_day)
    bank = ComponentPostingPlan(
        key="cash",
        kind="money_movement",
        facts={},
        entries=[Entry(account_code="1002", credit_fen=1000000 + interest_amount)],
    )
    commit_posting_plan(
        session,
        event=payment_parent,
        components=[principal, interest, bank],
        posting_date=due_day,
        description="同笔还本付息",
    )
    payments = list(session.scalars(select(BorrowingPayment)))
    assert {p.payment_kind for p in payments} == {"principal", "interest"}
    assert {p.event_id for p in payments} == {payment_parent.id}
    assert len({p.component_id for p in payments}) == 2


def test_business_fact_schema_excludes_execution_scope_and_float_rates():
    with pytest.raises(ValidationError):
        BorrowingDrawdownFacts(org_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        BorrowingDrawdownFacts(annual_rate_percent=3.65)
