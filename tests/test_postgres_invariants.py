from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.coa import get_account_by_role
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import BusinessEvent, BusinessEventComponent, Voucher, VoucherLine
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres]


def test_postgres_rejects_unbalanced_and_mutated_posted_vouchers():
    with authenticated_business_database("voucher_guards") as (engine, org_id, proof, authority):
        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="balanced nonzero lines"):
                with authority.attributed_call(session, tool_name="finance_direct_forgery_test"):
                    event = BusinessEvent(
                        org_id=org_id,
                        idempotency_key="unbalanced-direct-write",
                        event_type="composite",
                        status="draft",
                        facts={},
                        business_date=date(2026, 3, 5),
                        posting_date=date(2026, 3, 5),
                        rule_trace=[],
                    )
                    session.add(event)
                    session.flush()
                    component = BusinessEventComponent(
                        org_id=org_id,
                        event_id=event.id,
                        key="expense",
                        ordinal=1,
                        kind="expense",
                        facts={},
                        derived={},
                    )
                    voucher = Voucher(
                        org_id=org_id,
                        event_id=event.id,
                        voucher_number="202603-9999",
                        posting_date=event.posting_date,
                        description="unbalanced",
                        status="draft",
                    )
                    session.add_all([component, voucher])
                    session.flush()
                    for number, (role, debit, credit) in enumerate(
                        [("general_expense", 100, 0), ("accounts_payable", 0, 99)], 1
                    ):
                        session.add(
                            VoucherLine(
                                org_id=org_id,
                                voucher_id=voucher.id,
                                component_id=component.id,
                                line_number=number,
                                account_id=get_account_by_role(session, org_id, role).id,
                                debit_fen=debit,
                                credit_fen=credit,
                            )
                        )
                    session.flush()
                    voucher.status = "posted"
                session.commit()
            session.rollback()
            assert session.scalar(select(func.count()).select_from(Voucher)) == 0

            request = RecordEventRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "balanced-typed-write",
                    "posting_date": "2026-03-05",
                    "evidence_references": [proof],
                    "components": [
                        {
                            "key": "expense",
                            "kind": "expense",
                            "business_date": "2026-03-05",
                            "amount_fen": 100,
                            "expense_class": "general_expense",
                            "payment_basis": "supplier_credit",
                            "metadata": {"counterparty": {"kind": "supplier", "name": "Supplier"}},
                        }
                    ],
                }
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = FinanceService(session).record_event(request)
            assert posted.status == "posted", posted
            session.commit()
            voucher = session.get(Voucher, posted.voucher_id)
            voucher.description = "forbidden direct edit"
            with pytest.raises(DBAPIError):
                session.commit()
            session.rollback()
            assert session.get(Voucher, posted.voucher_id).description != "forbidden direct edit"
