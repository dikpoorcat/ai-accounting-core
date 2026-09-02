from __future__ import annotations

import shutil
from datetime import date
from typing import Any

import pytest
from alembic.config import Config
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.models import Evidence, OpenItem
from ai_accounting.schemas import RecordEventRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]

_POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"
)


def _request(org_id: object, payload: dict[str, Any]) -> RecordEventRequest:
    return RecordEventRequest.model_validate({"org_id": org_id, **payload})


def test_postgres_person_payment_on_behalf_and_cash_settlement_are_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer(_POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("FINANCE_ENVIRONMENT", "development")
        migration_config = Config("alembic.ini")
        migration_config.attributes["database_url_override"] = database_url
        command.upgrade(migration_config, "head")

        engine = create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="个人代垫 PostgreSQL 测试",
                    accounting_period_control_enabled=False,
                )
                authority = prepare_authenticated_bank_account(
                    session,
                    organization,
                    booking_date=date(2026, 8, 1),
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="a" * 64,
                    original_name="person-reimbursement-test.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/person-reimbursement-test.txt",
                    metadata_json={},
                )
                session.add(evidence)
                session.commit()

                service = FinanceService(session)
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    purchase = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-person-payable-source",
                                "event_type": "expense_payable",
                                "business_dates": {
                                    "business_date": "2026-08-01",
                                    "posting_date": "2026-08-01",
                                },
                                "counterparty": {"kind": "supplier", "name": "测试供应商"},
                                "amounts": {
                                    "gross_amount_fen": 50_000,
                                    "expense_account_role": "general_expense",
                                },
                            },
                        )
                    )
                assert purchase.status == "posted", purchase.errors
                session.commit()

                source_item = session.scalar(
                    select(OpenItem).where(OpenItem.source_event_id == purchase.event_id)
                )
                assert source_item is not None
                payer = {"kind": "employee", "name": "测试员工"}
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    on_behalf = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-person-paid-on-behalf",
                                "event_type": "employee_reimbursement",
                                "business_dates": {
                                    "business_date": "2026-08-02",
                                    "posting_date": "2026-08-02",
                                    "payment_date": "2026-08-02",
                                },
                                "counterparty": payer,
                                "amounts": {"gross_amount_fen": 50_000},
                                "details": {
                                    "paid_now": False,
                                    "reimbursement_kind": "existing_payable",
                                },
                                "allocations": [
                                    {
                                        "open_item_id": source_item.id,
                                        "amount_fen": 50_000,
                                    }
                                ],
                            },
                        )
                    )
                assert on_behalf.status == "posted", on_behalf.errors
                session.commit()

                person_item = session.scalar(
                    select(OpenItem).where(OpenItem.source_event_id == on_behalf.event_id)
                )
                assert person_item is not None
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    cash_payment = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-person-cash-settlement",
                                "event_type": "employee_reimbursement_payment",
                                "business_dates": {
                                    "business_date": "2026-08-02",
                                    "posting_date": "2026-08-02",
                                    "payment_date": "2026-08-02",
                                },
                                "counterparty": payer,
                                "amounts": {"amount_fen": 50_000},
                                "details": {"settlement_method": "cash"},
                                "allocations": [
                                    {
                                        "open_item_id": person_item.id,
                                        "amount_fen": 50_000,
                                    }
                                ],
                            },
                        )
                    )
                assert cash_payment.status == "posted", cash_payment.errors
                session.commit()
                assert source_item.status == "settled"
                assert person_item.status == "settled"

                reserve_bank = import_test_bank_transaction(
                    session,
                    organization,
                    amount_fen=-100_000,
                    key="person-owner-managed-reserve-source",
                    booking_date=date(2026, 8, 3),
                )
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    reserve_source = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-owner-managed-reserve-source",
                                "event_type": "expense_cash",
                                "business_dates": {
                                    "business_date": "2026-08-03",
                                    "posting_date": "2026-08-03",
                                    "payment_date": "2026-08-03",
                                },
                                "bank_account_code": "1002",
                                "bank_transaction_references": [{"id": reserve_bank.id}],
                                "amounts": {
                                    "gross_amount_fen": 100_000,
                                    "expense_account_role": "general_expense",
                                },
                            },
                        )
                    )
                assert reserve_source.status == "posted", reserve_source.errors
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    reserve_payable = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-reserve-payable-source",
                                "event_type": "expense_payable",
                                "business_dates": {
                                    "business_date": "2026-08-04",
                                    "posting_date": "2026-08-04",
                                },
                                "counterparty": {
                                    "kind": "supplier",
                                    "name": "备用金测试供应商",
                                },
                                "amounts": {
                                    "gross_amount_fen": 30_000,
                                    "expense_account_role": "general_expense",
                                },
                            },
                        )
                    )
                reserve_payable_item = session.scalar(
                    select(OpenItem).where(
                        OpenItem.source_event_id == reserve_payable.event_id
                    )
                )
                assert reserve_payable_item is not None
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    reserve_on_behalf = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-reserve-paid-on-behalf",
                                "event_type": "employee_reimbursement",
                                "business_dates": {
                                    "business_date": "2026-08-05",
                                    "posting_date": "2026-08-05",
                                    "payment_date": "2026-08-05",
                                },
                                "counterparty": payer,
                                "amounts": {"gross_amount_fen": 30_000},
                                "details": {
                                    "paid_now": False,
                                    "reimbursement_kind": "existing_payable",
                                },
                                "allocations": [
                                    {
                                        "open_item_id": reserve_payable_item.id,
                                        "amount_fen": 30_000,
                                    }
                                ],
                            },
                        )
                    )
                reserve_person_item = session.scalar(
                    select(OpenItem).where(
                        OpenItem.source_event_id == reserve_on_behalf.event_id
                    )
                )
                assert reserve_person_item is not None
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    reserve_payment = service.record_event(
                        _request(
                            organization.id,
                            {
                                "idempotency_key": "pg-owner-managed-reserve-settlement",
                                "event_type": "employee_reimbursement_payment",
                                "business_dates": {
                                    "business_date": "2026-08-31",
                                    "posting_date": "2026-08-31",
                                    "payment_date": "2026-08-31",
                                },
                                "counterparty": payer,
                                "amounts": {"amount_fen": 30_000},
                                "details": {
                                    "settlement_method": "owner_managed_reserve",
                                    "original_event_id": reserve_source.event_id,
                                },
                                "allocations": [
                                    {
                                        "open_item_id": reserve_person_item.id,
                                        "amount_fen": 30_000,
                                    }
                                ],
                            },
                        )
                    )
                assert reserve_payment.status == "posted", reserve_payment.errors
                session.commit()
                assert reserve_payable_item.status == "settled"
                assert reserve_person_item.status == "settled"
        finally:
            engine.dispose()
