from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_accounting_period_integration import _advance_refund_request, _customer_receipt_request
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    ConfirmBankStatementFileImportRequest,
    PreviewBankReconciliationScopeRequest,
    PreviewBankStatementFileImportRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorKind
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventDependency,
    Evidence,
    OrganizationDatabaseMetadata,
    Voucher,
)
from ai_accounting.schemas import BankTransactionReference
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]
IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"


@pytest.fixture(scope="module")
def postgres_engine():
    with PostgresContainer(IMAGE, driver="psycopg") as postgres:
        config = Config("alembic.ini")
        url = postgres.get_connection_url(driver="psycopg")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "0004_business_deletions")
        engine = create_engine(url)
        with engine.connect() as connection:
            with pytest.raises(DBAPIError, match="BUSINESS_EVENT_DEPENDENCY_INVALID"):
                connection.execute(
                    text("SELECT finance_business_event_amount(CAST(:facts AS jsonb))"),
                    {"facts": '{"amounts":{"gross_amount_fen":null,"amount_fen":50000}}'},
                )
            connection.rollback()
        command.upgrade(config, "head")
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.mark.parametrize(
    "amounts, expected",
    [
        ({"amount_fen": 1}, 1),
        ({"gross_amount_fen": None, "amount_fen": 50000}, 50000),
        ({"gross_amount_fen": 250, "amount_fen": None}, 250),
        ({"gross_amount_fen": 250, "amount_fen": 500}, 250),
        ({"gross_amount_fen": None, "amount_fen": 9223372036854775807}, 9223372036854775807),
    ],
)
def test_valid_amounts_keep_gross_precedence(postgres_engine, amounts, expected):
    with postgres_engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT finance_business_event_amount(CAST(:facts AS jsonb))"),
                {"facts": json.dumps({"amounts": amounts})},
            )
            == expected
        )


@pytest.mark.parametrize(
    "amounts",
    [
        {},
        {"gross_amount_fen": None},
        {"gross_amount_fen": None, "amount_fen": None},
        {"amount_fen": 0},
        {"amount_fen": -1},
        {"amount_fen": 1.5},
        {"amount_fen": "100"},
        {"amount_fen": True},
        {"amount_fen": []},
        {"amount_fen": {}},
        {"amount_fen": 9223372036854775808},
        {"gross_amount_fen": 0, "amount_fen": 100},
        {"gross_amount_fen": "invalid", "amount_fen": 100},
    ],
)
def test_invalid_amounts_are_rejected(postgres_engine, amounts):
    with postgres_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="BUSINESS_EVENT_DEPENDENCY_INVALID"):
            connection.execute(
                text("SELECT finance_business_event_amount(CAST(:facts AS jsonb))"),
                {"facts": json.dumps({"amounts": amounts})},
            )


def test_amount_only_receipt_refund_commits_and_preserves_usage_limit(postgres_engine, tmp_path):
    with Session(postgres_engine) as session:
        org = seed_organization(
            session,
            name="Refund regression",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        catalog_id = uuid.uuid4()
        session.add(
            OrganizationDatabaseMetadata(
                singleton_key=1,
                org_id=org.id,
                database_identity=uuid.uuid4(),
                current_catalog_instance_id=catalog_id,
                owner_approval_required=True,
            )
        )
        session.flush()
        context = ExecutionContext(
            org_id=org.id,
            owner_account_id=uuid.uuid4(),
            owner_session_id=uuid.uuid4(),
            owner_credential_version=1,
            executor_kind=ExecutorKind.AI_AGENT,
            executor_name="refund-regression",
            executor_version="1",
            request_correlation_id=uuid.uuid4(),
            catalog_instance_id=catalog_id,
        )

        def attributed(tool_name):
            return persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name=tool_name,
            )

        with attributed("finance_generate_accounting_period"):
            path = tmp_path / "scope.txt"
            path.write_bytes(b"scope")
            evidence = Evidence(
                org_id=org.id,
                sha256=hashlib.sha256(b"scope").hexdigest(),
                original_name="scope.txt",
                source="test",
                size_bytes=5,
                storage_path=str(path),
            )
            session.add(evidence)
            session.flush()
            generated = AccountingPeriodService(session).generate_accounting_period(
                GenerateAccountingPeriodRequest(
                    org_id=org.id,
                    period_month="2026-08",
                    idempotency_key="august",
                    confirmation_note="Test month",
                    evidence_references=[evidence.id],
                )
            )
            assert generated.status == "posted", generated
        with attributed("finance_confirm_bank_reconciliation_scope"):
            service = BankStatementService(session)
            scope = PreviewBankReconciliationScopeRequest(
                org_id=org.id,
                action_type="initial_confirmation",
                accounts=[
                    {
                        "bank_account_code": "1002",
                        "account_name": "银行存款",
                        "start_date": date(2026, 8, 1),
                    }
                ],
                explanation="Test scope",
                evidence_references=[evidence.id],
            )
            preview = service.preview_bank_reconciliation_scope(scope)
            assert preview.status == "calculated", preview
            confirmed = service.confirm_bank_reconciliation_scope(
                ConfirmBankReconciliationScopeRequest.model_validate(
                    scope.model_dump()
                    | {"calculation_hash": preview.calculation_hash, "idempotency_key": "scope"}
                )
            )
            assert confirmed.status == "posted", confirmed
        bank_service = BankStatementService(
            session,
            settings=Settings(
                finance_bank_import_dir=tmp_path, finance_evidence_dir=tmp_path / "evidence"
            ),
        )

        def bank(amount, day, key):
            filename = key + ".csv"
            (tmp_path / filename).write_text(
                "date,amount,reference\n" + f"{day},{Decimal(amount) / Decimal(100):.2f},{key}\n",
                encoding="utf-8",
            )
            request = PreviewBankStatementFileImportRequest(
                org_id=org.id,
                bank_account_code="1002",
                source_file_name=filename,
                file_format="csv",
                column_mapping={
                    "booking_date": "date",
                    "amount": "amount",
                    "external_id": "reference",
                },
            )
            preview = bank_service.preview_bank_statement_import(request)
            assert preview.status == "calculated", preview
            with attributed("finance_confirm_bank_statement_import"):
                result = bank_service.confirm_bank_statement_import(
                    ConfirmBankStatementFileImportRequest.model_validate(
                        request.model_dump()
                        | {"calculation_hash": preview.calculation_hash, "idempotency_key": key}
                    )
                )
                assert result.status == "posted", result
                return BankTransactionReference(id=result.data["imported_transaction_ids"][0])

        incoming = bank(120000, "2026-08-01", "receipt")
        outgoing = bank(-50000, "2026-08-03", "refund")
        too_much = bank(-70001, "2026-08-03", "excess-refund")
        remaining = bank(-70000, "2026-08-03", "remaining-refund")
        service = FinanceService(session)
        receipt_request = _customer_receipt_request(org).model_copy(
            update={"bank_transaction_references": [incoming]}
        )
        with attributed("finance_record_event"):
            receipt = service.record_event(receipt_request)
        assert receipt.status == "posted", receipt
        refund_request = _advance_refund_request(org, receipt.event_id).model_copy(
            update={"bank_transaction_references": [outgoing]}
        )
        assert refund_request.model_dump(mode="json")["amounts"]["gross_amount_fen"] is None
        with attributed("finance_record_event"):
            refund = service.record_event(refund_request)
        assert refund.status == "posted", refund
        session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        before_vouchers = session.scalar(select(func.count()).select_from(Voucher))
        with attributed("finance_record_event"):
            rejected = service.record_event(
                refund_request.model_copy(
                    update={
                        "idempotency_key": "over-limit",
                        "bank_transaction_references": [too_much],
                        "amounts": refund_request.amounts.model_copy(update={"amount_fen": 70001}),
                    }
                )
            )
        assert rejected.status == "rejected", rejected
        assert "refund exceeds" in str(rejected.errors)
        assert session.scalar(select(func.count()).select_from(Voucher)) == before_vouchers
        with attributed("finance_record_event"):
            final = service.record_event(
                refund_request.model_copy(
                    update={
                        "idempotency_key": "exact-remaining",
                        "bank_transaction_references": [remaining],
                        "amounts": refund_request.amounts.model_copy(update={"amount_fen": 70000}),
                    }
                )
            )
        assert final.status == "posted", final
        assert (
            session.scalar(
                select(func.sum(BusinessEventDependency.amount_fen)).where(
                    BusinessEventDependency.parent_event_id == receipt.event_id
                )
            )
            == 120000
        )
        event_id = refund.event_id
        session.commit()
    with Session(postgres_engine) as session:
        assert session.get(BusinessEvent, event_id).status == "posted"
