from __future__ import annotations

import uuid
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database
from conftest import prepare_authenticated_bank_account
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_enterprise_income_tax import change, confirm, payment, root
from test_tax_confirmation_components import _relief, _sale

from ai_accounting.coa import account_business_class
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.enterprise_income_tax import EnterpriseIncomeTaxService
from ai_accounting.enterprise_income_tax_schemas import QueryEnterpriseIncomeTaxRequest
from ai_accounting.models import (
    Account,
    BusinessEvent,
    BusinessEventComponent,
    BusinessEventDependency,
    EnterpriseIncomeTaxResult,
    Evidence,
    Organization,
    TaxPeriod,
    TaxPeriodSource,
    VoucherLine,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService
from ai_accounting.tax_accounts import period_liability_balances

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture
def postgres_database():
    with authenticated_business_database(
        "tax_confirmation_components", name="税务确认组件"
    ) as database:
        yield database


def test_postgres_pending_sale_empty_relief_and_payment_are_one_atomic_event(
    postgres_database,
):
    engine, org_id, evidence_id, authority = postgres_database
    request = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "pg-projected-tax-period-payment",
            "posting_date": date(2026, 3, 31),
            "description": "同事件税期与缴税",
            "evidence_references": [evidence_id],
            "components": [
                _relief("period", date(2026, 1, 1), date(2026, 3, 31)),
                _sale("sale", date(2026, 3, 5), 101),
                {
                    "key": "vat-payment",
                    "kind": "tax_settlement",
                    "business_date": date(2026, 3, 31),
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 1,
                    "tax_type": "vat",
                    "settlement_kind": "payment",
                    "period_start": date(2026, 1, 1),
                    "period_end": date(2026, 3, 31),
                    "assessment_component_key": "period",
                },
            ],
            "funds": [
                {
                    "key": "sale-cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 101,
                    "allocations": [{"component_key": "sale", "amount_fen": 101}],
                    "bank_transaction_references": [],
                },
                {
                    "key": "tax-cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 1,
                    "allocations": [{"component_key": "vat-payment", "amount_fen": 1}],
                    "bank_transaction_references": [],
                },
            ],
        }
    )
    with (
        Session(engine) as session,
        authority.attributed_call(session, tool_name="finance_preview_event"),
    ):
        before = session.scalar(select(func.count()).select_from(BusinessEvent))
        preview = ComponentService(session).preview(request)
        assert preview.status == "calculated", preview
        assert session.scalar(select(func.count()).select_from(BusinessEvent)) == before
        reviewed = RecordEventRequest.model_validate(preview.data["reviewed_request"])
        with authority.attributed_call(session, tool_name="finance_record_event"):
            posted = FinanceService(session).record_event(reviewed)
        assert posted.status == "posted", posted
        session.commit()

    with Session(engine) as session:
        period = session.scalar(select(TaxPeriod))
        assert period.calculation["vat_payable_fen"] == 1
        assert period.calculation["vat_relief_fen"] == 0
        assert period.calculation["surtax_total_fen"] == 0
        assert session.scalar(select(func.count()).select_from(TaxPeriodSource)) == 1
        relief = session.scalar(
            select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == period.adjustment_event_id,
                BusinessEventComponent.key == "period",
            )
        )
        assert (
            relief.derived["source_review_proofs"] == period.calculation["source_review_snapshots"]
        )
        assert relief.derived["_posting_entries"] == []


def test_postgres_two_disjoint_tax_periods_reverse_with_shared_source_event(
    postgres_database,
):
    engine, org_id, evidence_id, authority = postgres_database
    request = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "pg-two-projected-periods",
            "posting_date": date(2026, 6, 30),
            "description": "同事件两期税务确认",
            "evidence_references": [evidence_id],
            "components": [
                _relief("q2-period", date(2026, 4, 1), date(2026, 6, 30)),
                _relief("q1-period", date(2026, 1, 1), date(2026, 3, 31)),
                _sale("q1-sale", date(2026, 3, 5), 101, invoice_type="ordinary"),
                _sale("q2-sale", date(2026, 6, 5), 101, invoice_type="ordinary"),
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": date(2026, 6, 30),
                    "amount_fen": 202,
                    "allocations": [
                        {"component_key": "q1-sale", "amount_fen": 101},
                        {"component_key": "q2-sale", "amount_fen": 101},
                    ],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    with (
        Session(engine) as session,
        authority.attributed_call(session, tool_name="finance_preview_event"),
    ):
        preview = ComponentService(session).preview(request)
        assert preview.status == "calculated", preview
        with authority.attributed_call(session, tool_name="finance_record_event"):
            posted = FinanceService(session).record_event(
                RecordEventRequest.model_validate(preview.data["reviewed_request"])
            )
        assert posted.status == "posted", posted
        event_id = posted.event_id
        session.commit()

    with (
        Session(engine) as session,
        authority.attributed_call(session, tool_name="finance_reverse_event"),
    ):
        periods = list(session.scalars(select(TaxPeriod).order_by(TaxPeriod.start_date)))
        assert [period.calculation["vat_relief_fen"] for period in periods] == [1, 1]
        assert [len(period.sources) for period in periods] == [1, 1]
        reversed_result = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=org_id,
                event_id=event_id,
                posting_date=date(2026, 7, 1),
                reason="撤销两期组合确认",
                idempotency_key="reverse-pg-two-projected-periods",
            )
        )
        assert reversed_result.status == "posted", reversed_result
        session.commit()

    with Session(engine) as session:
        periods = list(session.scalars(select(TaxPeriod)))
        assert len(periods) == 2
        assert all(period.status == "reversed" for period in periods)


def test_later_payment_uses_exact_period_component_from_mixed_confirmation(
    postgres_database,
):
    engine, org_id, evidence_id, authority = postgres_database
    source_request = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "pg-mixed-tax-confirmation",
            "posting_date": date(2026, 3, 31),
            "description": "销售与税期确认",
            "evidence_references": [evidence_id],
            "components": [
                _relief("period", date(2026, 1, 1), date(2026, 3, 31)),
                _sale("sale", date(2026, 3, 5), 101),
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 101,
                    "allocations": [{"component_key": "sale", "amount_fen": 101}],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    with (
        Session(engine) as session,
        authority.attributed_call(session, tool_name="finance_preview_event"),
    ):
        preview = ComponentService(session).preview(source_request)
        assert preview.status == "calculated", preview
        with authority.attributed_call(session, tool_name="finance_record_event"):
            source = FinanceService(session).record_event(
                RecordEventRequest.model_validate(preview.data["reviewed_request"])
            )
        assert source.status == "posted", source
        session.commit()

    payment_request = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "pg-later-tax-payment",
            "posting_date": date(2026, 4, 1),
            "description": "后续缴纳已确认税款",
            "evidence_references": [evidence_id],
            "components": [
                {
                    "key": "payment",
                    "kind": "tax_settlement",
                    "business_date": date(2026, 4, 1),
                    "payment_date": date(2026, 4, 1),
                    "amount_fen": 1,
                    "tax_type": "vat",
                    "settlement_kind": "payment",
                    "period_start": date(2026, 1, 1),
                    "period_end": date(2026, 3, 31),
                }
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": date(2026, 4, 1),
                    "amount_fen": 1,
                    "allocations": [{"component_key": "payment", "amount_fen": 1}],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    with (
        Session(engine) as session,
        authority.attributed_call(session, tool_name="finance_record_event"),
    ):
        paid = FinanceService(session).record_event(payment_request)
        assert paid.status == "posted", paid
        session.commit()

    with Session(engine) as session:
        period = session.scalar(select(TaxPeriod))
        payment_component = session.scalar(
            select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == paid.event_id,
                BusinessEventComponent.key == "payment",
            )
        )
        dependency = session.scalar(
            select(BusinessEventDependency).where(
                BusinessEventDependency.child_component_id == payment_component.id,
                BusinessEventDependency.parent_event_id == period.adjustment_event_id,
                BusinessEventDependency.parent_component_id == period.component_id,
            )
        )
        assert dependency is not None
        assert dependency.parent_component_id == period.component_id


def test_postgres_disjoint_period_balances_ignore_sibling_tax_components(
    postgres_database,
):
    engine, org_id, evidence_id, authority = postgres_database
    confirmation_request = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "pg-disjoint-period-account-scope",
            "posting_date": date(2026, 6, 30),
            "evidence_references": [evidence_id],
            "components": [
                _relief("q1-period", date(2026, 1, 1), date(2026, 3, 31)),
                _relief("q2-period", date(2026, 4, 1), date(2026, 6, 30)),
                _sale("q1-sale", date(2026, 3, 5), 101),
                _sale("q2-sale", date(2026, 6, 5), 202),
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": date(2026, 6, 30),
                    "amount_fen": 303,
                    "allocations": [
                        {"component_key": "q1-sale", "amount_fen": 101},
                        {"component_key": "q2-sale", "amount_fen": 202},
                    ],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    with (
        Session(engine) as session,
        authority.attributed_call(session, tool_name="finance_preview_event"),
    ):
        preview = ComponentService(session).preview(confirmation_request)
        assert preview.status == "calculated", preview
        with authority.attributed_call(session, tool_name="finance_record_event"):
            confirmed = FinanceService(session).record_event(
                RecordEventRequest.model_validate(preview.data["reviewed_request"])
            )
        assert confirmed.status == "posted", confirmed
        session.commit()

    for key, start, end, amount in (
        ("q1", date(2026, 1, 1), date(2026, 3, 31), 1),
        ("q2", date(2026, 4, 1), date(2026, 6, 30), 2),
    ):
        payment_request = RecordEventRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": f"pg-disjoint-period-{key}-payment",
                "posting_date": date(2026, 7, 1),
                "evidence_references": [evidence_id],
                "components": [
                    {
                        "key": "payment",
                        "kind": "tax_settlement",
                        "business_date": date(2026, 7, 1),
                        "payment_date": date(2026, 7, 1),
                        "amount_fen": amount,
                        "tax_type": "vat",
                        "settlement_kind": "payment",
                        "period_start": start,
                        "period_end": end,
                    }
                ],
                "funds": [
                    {
                        "key": "cash",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": date(2026, 7, 1),
                        "amount_fen": amount,
                        "allocations": [{"component_key": "payment", "amount_fen": amount}],
                        "bank_transaction_references": [],
                    }
                ],
            }
        )
        with (
            Session(engine) as session,
            authority.attributed_call(session, tool_name="finance_record_event"),
        ):
            paid = FinanceService(session).record_event(payment_request)
            assert paid.status == "posted", paid
            session.commit()

    with Session(engine) as session:
        periods = list(session.scalars(select(TaxPeriod).order_by(TaxPeriod.start_date)))
        assert [period.calculation["vat_payable_fen"] for period in periods] == [1, 2]
        assert periods[0].adjustment_event_id == periods[1].adjustment_event_id
        assert periods[0].component_id != periods[1].component_id
        legacy_event_vat_delta = sum(
            line.credit_fen - line.debit_fen
            for line, account in session.execute(
                select(VoucherLine, Account)
                .join(Account, Account.id == VoucherLine.account_id)
                .join(
                    BusinessEventComponent,
                    BusinessEventComponent.id == VoucherLine.component_id,
                )
                .where(
                    VoucherLine.org_id == org_id,
                    BusinessEventComponent.event_id == periods[0].adjustment_event_id,
                )
            )
            if account_business_class(account) == "vat_payable"
        )
        assert legacy_event_vat_delta == 3
        assert [
            period_liability_balances(session, org_id, period, "vat") for period in periods
        ] == [{}, {}]


def test_postgres_reversed_cit_result_restores_ancestor_and_allows_next_revision(
    postgres_database,
):
    engine, org_id, evidence_id, authority = postgres_database
    with Session(engine) as session:
        organization = session.get(Organization, org_id)
        evidence = session.get(Evidence, evidence_id)
        prepare_authenticated_bank_account(
            session,
            organization,
            booking_date=date(2026, 6, 28),
            authority=authority,
            evidence_id=evidence_id,
        )
        with authority.attributed_call(session, tool_name="finance_confirm_enterprise_income_tax"):
            root_id = root(session, organization, evidence)
        paid, _, _ = payment(
            session,
            organization,
            evidence,
            root_id,
            3_000,
            key="pg-paid-before-result-reversal",
            posting_date=date(2026, 7, 10),
        )
        assert paid.status == "posted", paid
        prepare_authenticated_bank_account(
            session,
            organization,
            booking_date=date(2026, 8, 5),
            authority=authority,
            evidence_id=evidence_id,
        )
        service = EnterpriseIncomeTaxService(session)
        with authority.attributed_call(
            session, tool_name="finance_confirm_enterprise_income_tax_result"
        ):
            revised, _ = confirm(service, change(organization, evidence, root_id))
        first_result_id = revised["result_id"]
        first_event_id = revised["event_id"]
        session.commit()

    with Session(engine) as session:
        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reversed_result = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=org_id,
                    event_id=first_event_id,
                    posting_date=date(2026, 8, 6),
                    reason="撤销最新企业所得税申报结果",
                    idempotency_key="pg-reverse-latest-cit-result",
                )
            )
        assert reversed_result.status == "posted", reversed_result
        service = EnterpriseIncomeTaxService(session)
        current = service.query(QueryEnterpriseIncomeTaxRequest(org_id=org_id))["data"]
        assert len(current["sources"]) == 1
        restored = current["sources"][0]
        assert restored["source_id"] == str(root_id)
        assert restored["recognized_tax_fen"] == 10_000
        assert restored["net_paid_fen"] == 3_000
        assert restored["payable_fen"] == 7_000
        assert session.scalar(
            text("SELECT finance_cit_confirmation_effective(:root_id, DATE '2026-09-30')"),
            {"root_id": root_id},
        )
        organization = session.get(Organization, org_id)
        evidence = session.get(Evidence, evidence_id)
        with authority.attributed_call(
            session, tool_name="finance_confirm_enterprise_income_tax_result"
        ):
            replacement, _ = confirm(
                service,
                change(organization, evidence, root_id, declared_tax_fen=12_000),
                key="pg-result-after-reversal",
            )
        replacement_row = session.get(
            EnterpriseIncomeTaxResult,
            uuid.UUID(replacement["result_id"]),
        )
        assert replacement_row.revision == 2
        assert replacement_row.previous_result_id is None
        assert replacement["data"]["expense_adjustment_fen"] == 2_000
        assert replacement["data"]["net_paid_fen"] == 3_000
        assert replacement["data"]["payable_fen"] == 9_000
        first_result = session.get(EnterpriseIncomeTaxResult, uuid.UUID(first_result_id))
        assert first_result.revision == 1
        session.commit()
