from __future__ import annotations

import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
import test_event_amendments as cases
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_service import sale_request
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.event_amendments import EventAmendmentService, _graph, _json
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorKind
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventAmendment,
    Evidence,
    Organization,
    OrganizationDatabaseMetadata,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]
IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"


def banked_checks(engine, context, evidence_id, tmp_path):
    from test_borrowing_service import _draw_request
    from test_payroll_service import payment_request, preview_and_confirm

    from ai_accounting.bank_statement_schemas import (
        ConfirmBankReconciliationScopeRequest,
        ConfirmBankStatementFileImportRequest,
        PreviewBankReconciliationScopeRequest,
        PreviewBankStatementFileImportRequest,
    )
    from ai_accounting.bank_statement_service import BankStatementService
    from ai_accounting.borrowing_service import BorrowingService
    from ai_accounting.config import Settings
    from ai_accounting.labor_remuneration_schemas import (
        ConfirmUnifiedPayoutRunRequest,
        PreviewUnifiedPayoutRunRequest,
    )
    from ai_accounting.labor_remuneration_service import LaborRemunerationService
    from ai_accounting.models import Account, BankTransaction, OpenItem

    def attributed(session, tool):
        return persist_execution_attribution(
            session,
            context=replace(context, request_correlation_id=uuid.uuid4()),
            tool_name=tool,
        )

    with (
        Session(engine) as session,
        session.begin(),
        attributed(session, "finance_confirm_bank_reconciliation_scope"),
    ):
        service = BankStatementService(session)
        request = PreviewBankReconciliationScopeRequest(
            org_id=context.org_id,
            action_type="initial_confirmation",
            accounts=[
                {
                    "bank_account_code": "1002",
                    "account_name": session.scalar(
                        select(Account.name).where(
                            Account.org_id == context.org_id, Account.code == "1002"
                        )
                    ),
                    "start_date": date(2026, 1, 1),
                }
            ],
            explanation="Test scope",
            evidence_references=[evidence_id],
        )
        preview = service.preview_bank_reconciliation_scope(request)
        assert preview.status == "calculated", preview
        result = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                request.model_dump()
                | {"calculation_hash": preview.calculation_hash, "idempotency_key": "bank-scope"}
            )
        )
        assert result.status == "posted", result

    def import_bank(session, amount, booking_date, key):
        filename = key + ".csv"
        (tmp_path / filename).write_text(
            f"date,amount,reference\n{booking_date},{Decimal(amount) / Decimal(100):.2f},{key}\n",
            encoding="utf-8",
        )
        service = BankStatementService(
            session,
            settings=Settings(
                finance_bank_import_dir=tmp_path,
                finance_evidence_dir=tmp_path / "evidence",
            ),
        )
        request = PreviewBankStatementFileImportRequest(
            org_id=context.org_id,
            bank_account_code="1002",
            source_file_name=filename,
            file_format="csv",
            column_mapping={"booking_date": "date", "amount": "amount", "external_id": "reference"},
        )
        preview = service.preview_bank_statement_import(request)
        assert preview.status == "calculated", preview
        with attributed(session, "finance_confirm_bank_statement_import"):
            result = service.confirm_bank_statement_import(
                ConfirmBankStatementFileImportRequest.model_validate(
                    request.model_dump()
                    | {"calculation_hash": preview.calculation_hash, "idempotency_key": key}
                )
            )
            assert result.status == "posted", result
        return session.get(BankTransaction, uuid.UUID(result.data["imported_transaction_ids"][0]))

    for kind in ("borrowing", "salary", "payout"):
        with Session(engine) as session:
            transaction = session.begin()
            org = session.get(Organization, context.org_id)
            amount, day = {
                "borrowing": (1_000_000, "2026-01-01"),
                "salary": (-839_500, "2026-03-05"),
                "payout": (-420_000, "2026-09-05"),
            }[kind]
            bank = import_bank(session, amount, day, kind)
            with attributed(session, "finance_amend_event"):
                if kind == "borrowing":
                    request = _draw_request(org, session.get(Evidence, evidence_id), bank)
                    source = BorrowingService(session).draw_borrowing(request)
                    replacement = request.model_copy(update={"contract_name": "Corrected loan"})
                elif kind == "salary":
                    _, payroll = preview_and_confirm(session, org)
                    item = session.scalar(
                        select(OpenItem).where(
                            OpenItem.source_event_id == payroll.event_id,
                            OpenItem.payable_category == "salary",
                        )
                    )
                    request = payment_request(
                        org,
                        event_type="salary_payment",
                        amount_fen=839_500,
                        allocations=[{"open_item_id": item.id, "amount_fen": 1_000_000}],
                        salary_withholdings=[
                            {
                                "open_item_id": item.id,
                                "employee_social_insurance_items": {"pension": 80_000},
                                "employee_housing_fund_items": {"housing_fund": 70_000},
                                "individual_income_tax_fen": 10_500,
                            }
                        ],
                        bank=bank,
                        key="salary",
                    )
                    source = FinanceService(session).record_event(request)
                    replacement = request.model_copy(update={"description": "Corrected salary"})
                else:
                    cases.test_labor_batch_recalculates_tax_and_preserves_batch(session, org)
                    item = session.scalar(
                        select(OpenItem).where(OpenItem.payable_category == "labor_remuneration")
                    )
                    request = PreviewUnifiedPayoutRunRequest.model_validate(
                        {
                            "org_id": org.id,
                            "idempotency_key": "payout-preview",
                            "business_date": day,
                            "payment_date": day,
                            "posting_date": day,
                            "bank_account_code": "1002",
                            "bank_transaction_id": bank.id,
                            "labor_items": [
                                {
                                    "source_open_item_id": item.id,
                                    "settlement_mode": "net_after_withholding",
                                }
                            ],
                            "withholding_agency_code": "tax-office",
                            "withholding_agency_name": "Tax office",
                            "evidence_references": [evidence_id],
                        }
                    )
                    service = LaborRemunerationService(session)
                    preview = service.preview_payout(request)
                    assert preview.status == "calculated", preview
                    source = service.confirm_payout(
                        ConfirmUnifiedPayoutRunRequest(
                            org_id=org.id,
                            payout_run_id=preview.payout_run_id,
                            idempotency_key="payout-confirm",
                            calculation_hash=preview.calculation_hash,
                            confirmation_note="Confirm payout",
                        )
                    )
                    replacement = request.model_copy(update={"description": "Corrected payout"})
                assert source.status == "posted", source
                result = EventAmendmentService(session).amend(
                    cases.amendment(session, source, replacement, key=kind + "-amend")
                )
                assert result["status"] == "posted", result
                assert result["voucher_id"] == str(source.voucher_id)
                assert session.get(BankTransaction, bank.id).matched_event_id == source.event_id
                session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            transaction.rollback()


def test_forward_migration_and_all_posting_families(tmp_path):
    with PostgresContainer(IMAGE, driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(url)
        with Session(engine) as session, session.begin():
            org = seed_organization(
                session,
                name="Amendments",
                taxpayer_identification_number="91330106MA1234567T",
                accounting_period_control_enabled=False,
            )
            org_id = org.id
            catalog_id = uuid.uuid4()
            session.add(
                OrganizationDatabaseMetadata(
                    singleton_key=1,
                    org_id=org_id,
                    database_identity=uuid.uuid4(),
                    current_catalog_instance_id=catalog_id,
                    owner_approval_required=True,
                )
            )
        context = ExecutionContext(
            org_id=org_id,
            owner_account_id=uuid.uuid4(),
            owner_session_id=uuid.uuid4(),
            owner_credential_version=1,
            executor_kind=ExecutorKind.AI_AGENT,
            executor_name="amend-test",
            executor_version="1",
            request_correlation_id=uuid.uuid4(),
            catalog_instance_id=catalog_id,
        )
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=context,
                tool_name="finance_generate_accounting_period",
            ),
        ):
            path = tmp_path / "period.txt"
            path.write_bytes(b"period")
            evidence = Evidence(
                org_id=org_id,
                sha256=hashlib.sha256(b"period").hexdigest(),
                original_name="period.txt",
                source="test",
                size_bytes=6,
                storage_path=str(path),
            )
            session.add(evidence)
            session.flush()
            evidence_id = evidence.id
            for month in range(1, 10):
                result = AccountingPeriodService(session).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month=f"2026-{month:02d}",
                        idempotency_key=f"month-{month}",
                        confirmation_note="Test period",
                        evidence_references=[evidence.id],
                    )
                )
                assert result.status == "posted", result
        checks = [
            (cases.test_sale_replaces_voucher_and_open_item_with_audit_and_replay, ()),
            (
                cases.test_asset_acquisition_amendment_keeps_card_identity_and_recalculates_cost,
                ("fixed",),
            ),
            (
                cases.test_asset_acquisition_amendment_keeps_card_identity_and_recalculates_cost,
                ("intangible",),
            ),
            (cases.test_payroll_amendment_recalculates_same_batch_and_liabilities, ()),
        ]
        checks += [
            (cases.test_labor_batch_recalculates_tax_and_preserves_batch, ()),
            (cases.test_tax_snapshot_amendment_and_locked_source, ()),
            (cases.test_enterprise_income_tax_confirmation_amendment, ()),
        ]
        checks += [(cases.test_income_tax_result_amendment_reuses_prior_reversal, ())]
        checks += [
            (cases.test_annual_bonus_amendment_preserves_tax_state, (method,))
            for method in ("combined", "separate")
        ]
        checks += [
            (cases.test_asset_lifecycle_amendment, (kind,))
            for kind in (
                "activation",
                "depreciation",
                "batch",
                "disposal",
                "amortization",
                "retirement",
            )
        ]
        for check, args in checks:
            with Session(engine) as session:
                transaction = session.begin()
                with persist_execution_attribution(
                    session,
                    context=replace(context, request_correlation_id=uuid.uuid4()),
                    tool_name="finance_amend_event",
                ):
                    check(session, session.get(Organization, org_id), *args)
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                transaction.rollback()
        banked_checks(engine, context, evidence_id, tmp_path)
        with (
            Session(engine) as session,
            session.begin(),
            persist_execution_attribution(
                session,
                context=replace(context, request_correlation_id=uuid.uuid4()),
                tool_name="finance_record_event",
            ),
        ):
            source_request = sale_request(
                session.get(Organization, org_id), event_type="service_credit_sale"
            )
            source = FinanceService(session).record_event(source_request)
            request = cases.amendment(
                session,
                source,
                source_request.model_copy(update={"description": "Edited"}),
                key="concurrent-edit",
            )

        def writer(key):
            with (
                Session(engine) as session,
                session.begin(),
                persist_execution_attribution(
                    session,
                    context=replace(context, request_correlation_id=uuid.uuid4()),
                    tool_name="finance_amend_event",
                ),
            ):
                return EventAmendmentService(session).amend(
                    request.model_copy(update={"idempotency_key": key})
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ["same-edit", "same-edit"]))
        assert all(result["status"] == "posted" for result in results), results
        assert sum(bool(result.get("idempotent_replay")) for result in results) == 1
        with Session(engine) as session:
            source_event = session.get(BusinessEvent, source.event_id)
            assert source_event.description == "Edited"
            request = cases.amendment(
                session,
                source,
                source_request.model_copy(update={"description": "Second edit"}),
                key="conflict",
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, ["edit-a", "edit-b"]))
        assert sorted(result["status"] for result in results) == ["posted", "rejected"]
        assert next(result for result in results if result["status"] == "rejected")["errors"] == [
            "AMENDMENT_FACTS_STALE"
        ]

        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="immutable"):
                session.execute(
                    text("UPDATE business_events SET status='draft' WHERE id=:id"),
                    {"id": source.event_id},
                )
            session.rollback()
            with pytest.raises(DBAPIError, match="AMENDMENT_AUDIT_IMMUTABLE"):
                session.execute(
                    text(
                        "UPDATE business_event_amendments SET reason='tampered' WHERE event_id=:id"
                    ),
                    {"id": source.event_id},
                )
            session.rollback()

        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="AMENDMENT_INCOMPLETE"):
                with (
                    session.begin(),
                    persist_execution_attribution(
                        session,
                        context=replace(context, request_correlation_id=uuid.uuid4()),
                        tool_name="finance_amend_event",
                    ),
                ):
                    source_event = session.get(BusinessEvent, source.event_id)
                    session.add(
                        BusinessEventAmendment(
                            org_id=org_id,
                            event_id=source.event_id,
                            revision=3,
                            idempotency_key="incomplete",
                            request_hash="a" * 64,
                            reason="Incomplete",
                            before_state={"tables": _json(_graph(session, source_event))},
                        )
                    )
                    session.flush()
                    source_event.status = "draft"
                    session.flush()
                    session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        engine.dispose()
