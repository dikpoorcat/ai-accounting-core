from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from sqlalchemy.orm import Session

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import BusinessEvent, PayrollLine

pytestmark = pytest.mark.postgres


def test_postgres_monthly_recognition_and_direct_settlement_guard(monkeypatch):
    with authenticated_business_database("fact_precision") as (engine, org_id, evidence_id, owner):
        with Session(engine) as session:
            with owner.attributed_call(session, tool_name="finance_record_event"):
                result = ComponentService(session).record(
                    RecordEventRequest(
                        org_id=org_id,
                        idempotency_key="monthly",
                        posting_date="2026-06-30",
                        evidence_references=[evidence_id],
                        components=[
                            {
                                "key": "cost",
                                "kind": "expense",
                                "recognition_period": "2026-06",
                                "expense_class": "general_expense",
                                "payment_basis": "supplier_credit",
                                "amount_fen": 100,
                            }
                        ],
                    )
                )
                assert result.status == "posted", result
            session.commit()
            source_id = result.event_id
            # Bypass the compiler check deliberately to exercise the deferred SQL guard.
            monkeypatch.setattr(ComponentService, "validate_fund_source_dates", lambda *args: None)
            with pytest.raises(sa.exc.DBAPIError, match="MONTHLY_SOURCE_NOT_RECOGNIZED"):
                with owner.attributed_call(session, tool_name="finance_record_event"):
                    result = ComponentService(session).record(
                        RecordEventRequest(
                            org_id=org_id,
                            idempotency_key="early",
                            posting_date="2026-07-07",
                            evidence_references=[evidence_id],
                            components=[
                                {
                                    "key": "pay",
                                    "kind": "payable_settlement",
                                    "business_date": "2026-06-30",
                                    "allocations": [
                                        {
                                            "source_event_key": "monthly",
                                            "source_component_key": "cost",
                                            "amount_fen": 100,
                                        }
                                    ],
                                }
                            ],
                            funds=[
                                {
                                    "key": "cash",
                                    "account_code": "1001",
                                    "direction": "payment",
                                    "payment_date": "2026-06-15",
                                    "amount_fen": 100,
                                    "allocations": [{"component_key": "pay", "amount_fen": 100}],
                                }
                            ],
                        )
                    )
                    assert result.status == "posted", result
                session.commit()
            session.rollback()
            assert session.scalars(sa.select(BusinessEvent.id)).all() == [source_id]


def test_postgres_wage_scope_uses_accounting_applicability():
    with authenticated_business_database("fact_wage_scope") as (engine, org_id, evidence_id, owner):
        with Session(engine) as session:
            confirmed_payroll(session, org_id, evidence_id, owner)
            session.commit()
            lines = session.scalars(sa.select(PayrollLine)).all()
            assert lines and {line.wage_tax_scope for line in lines} == {"wage_income"}
            assert all(line.gross_salary_fen > 0 for line in lines)


def test_postgres_period_cutoff_guard_rejects_future_month(monkeypatch):
    with authenticated_business_database("fact_future_month") as (
        engine,
        org_id,
        evidence_id,
        owner,
    ):
        with Session(engine) as session:
            request = RecordEventRequest(
                org_id=org_id,
                idempotency_key="future-month",
                posting_date="2026-06-30",
                evidence_references=[evidence_id],
                components=[
                    {
                        "key": "cost",
                        "kind": "expense",
                        "recognition_period": "2026-06",
                        "expense_class": "general_expense",
                        "payment_basis": "supplier_credit",
                        "amount_fen": 100,
                    }
                ],
            )
            prepare = ComponentService.prepare_request

            def bypass(self, value):
                normalized = prepare(self, value)
                normalized.posting_date = date(2026, 6, 15)
                return normalized

            monkeypatch.setattr(ComponentService, "prepare_request", bypass)
            with pytest.raises(sa.exc.DBAPIError, match="RECOGNITION_PERIOD_IN_FUTURE"):
                with owner.attributed_call(session, tool_name="finance_record_event"):
                    result = ComponentService(session).record(request)
                    assert result.status == "posted", result
                session.commit()
            session.rollback()
            assert not session.scalars(sa.select(BusinessEvent)).all()
