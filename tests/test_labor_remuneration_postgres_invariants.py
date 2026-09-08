from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
from threading import Barrier

import pytest
from _postgres_helpers import catalog_owner_authority
from alembic.config import Config
from conftest import (
    AuthenticatedOwnerAuthority,
    bind_authenticated_bank_account,
    import_test_bank_transaction,
)
from sqlalchemy import create_engine, func, inspect, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    PreviewBankReconciliationScopeRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.database import make_session_factory
from ai_accounting.labor_remuneration_schemas import (
    ConfirmLaborRemunerationBatchRequest,
    LaborRemunerationItemFacts,
    PreviewLaborRemunerationBatchRequest,
    RegisterLaborServicePersonRequest,
)
from ai_accounting.labor_remuneration_service import LaborRemunerationService
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    Evidence,
    LaborRemunerationLine,
    OpenItem,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]

POSTGRES_IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"  # noqa: E501


def _evidence(session, org_id, marker: str) -> Evidence:
    evidence = Evidence(
        org_id=org_id,
        sha256=marker * 64,
        original_name=f"{marker}.txt",
        media_type="text/plain",
        source="postgres-labor-invariant-test",
        size_bytes=1,
        storage_path=f"tests/labor/{marker}.txt",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()
    return evidence


def _confirm_bank_scope(
    session,
    *,
    org_id,
    evidence_id,
    authority: AuthenticatedOwnerAuthority,
) -> None:
    request = PreviewBankReconciliationScopeRequest(
        org_id=org_id,
        action_type="initial_confirmation",
        accounts=[
            {
                "bank_account_code": "1002",
                "account_name": "银行存款",
                "start_date": date(2026, 3, 1),
            }
        ],
        explanation="PostgreSQL 劳务测试确认银行范围",
        evidence_references=[evidence_id],
    )
    service = BankStatementService(session)
    preview = service.preview_bank_reconciliation_scope(request)
    with authority.attributed_call(
        session, tool_name="finance_confirm_bank_reconciliation_scope"
    ):
        result = service.confirm_bank_reconciliation_scope(
            ConfirmBankReconciliationScopeRequest.model_validate(
                request.model_dump()
                | {
                    "calculation_hash": preview.calculation_hash,
                    "idempotency_key": "pg-labor-bank-scope",
                }
            )
        )
    assert result.status == "posted", result
    session.commit()
    bind_authenticated_bank_account(session, authority)


def test_postgres_formal_baseline_installs_labor_invariants() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        config.attributes["database_url_override"] = database_url
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        try:
            component_columns = {
                column["name"]
                for column in inspect(engine).get_columns("business_event_components")
            }
            assert {"event_id", "key", "kind", "facts", "derived"} <= component_columns
            assert "component_id" in {
                column["name"] for column in inspect(engine).get_columns("voucher_lines")
            }
            assert "source_component_id" in {
                column["name"] for column in inspect(engine).get_columns("open_items")
            }

        finally:
            engine.dispose()


def test_postgres_labor_accrual_and_gross_unwithheld_payout_commit_end_to_end() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        config.attributes["database_url_override"] = database_url
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        factory = make_session_factory(engine)
        authority_stack = ExitStack()
        try:
            with factory() as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="个人劳务 PostgreSQL 终态事件端到端测试",
                    accounting_period_control_enabled=False,
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(session, organization)
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    evidence = _evidence(session, organization.id, "b")
                session.commit()
                _confirm_bank_scope(
                    session,
                    org_id=organization.id,
                    evidence_id=evidence.id,
                    authority=authority,
                )
                import_test_bank_transaction(
                    session,
                    organization,
                    amount_fen=1,
                    key="pg-labor-period-anchor",
                    booking_date=date(2026, 3, 31),
                )
                payout_bank = import_test_bank_transaction(
                    session,
                    organization,
                    amount_fen=-500_000,
                    key="pg-gross-unwithheld-bank",
                    booking_date=date(2026, 4, 4),
                )
                service = LaborRemunerationService(session)
                with authority.attributed_call(
                    session, tool_name="finance_register_labor_service_person"
                ):
                    registered = service.register_person(
                        RegisterLaborServicePersonRequest(
                        org_id=organization.id,
                        idempotency_key="pg-gross-labor-person",
                        person_code="PG-GROSS-001",
                        name="PostgreSQL 毛额支付劳务人员",
                        relationship_start_date=date(2026, 3, 1),
                        status="active",
                        evidence_references=[evidence.id],
                        )
                    )
                with authority.attributed_call(
                    session, tool_name="finance_preview_labor_remuneration_batch"
                ):
                    batch_preview = service.preview_batch(
                        PreviewLaborRemunerationBatchRequest(
                        org_id=organization.id,
                        idempotency_key="pg-gross-labor-preview",
                        remuneration_period="2026-03",
                        business_date=date(2026, 3, 31),
                        posting_date=date(2026, 3, 31),
                        planned_payment_date=date(2026, 4, 4),
                        items=[
                            LaborRemunerationItemFacts(
                                labor_person_id=registered.labor_person_id,
                                service_start_date=date(2026, 3, 1),
                                service_end_date=date(2026, 3, 31),
                                fixed_fee_fen=300_000,
                                commission_fen=200_000,
                                expense_role="labor_service_cost",
                                tax_identity="resident",
                                income_grouping="continuous_monthly",
                                is_full_time_student=False,
                                external_declaration_status="not_due",
                            )
                        ],
                        evidence_references=[evidence.id],
                        )
                    )
                assert batch_preview.status.value == "calculated", batch_preview
                with authority.attributed_call(
                    session, tool_name="finance_confirm_labor_remuneration_batch"
                ):
                    accrual = service.confirm_batch(
                        ConfirmLaborRemunerationBatchRequest(
                        org_id=organization.id,
                        batch_id=batch_preview.batch_id,
                        idempotency_key="pg-gross-labor-confirm",
                        calculation_hash=batch_preview.calculation_hash,
                        confirmation_note="确认 PostgreSQL 劳务计提终态事件",
                    )
                )
                assert accrual.status.value == "posted"
                session.commit()

                labor_open_item = session.scalar(
                    select(OpenItem).where(
                        OpenItem.org_id == organization.id,
                        OpenItem.source_event_id == accrual.event_id,
                        OpenItem.payable_category == "labor_remuneration",
                    )
                )
                assert labor_open_item is not None
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    payout = ComponentService(session).record(RecordEventRequest.model_validate({
                    "org_id": organization.id,
                    "idempotency_key": "pg-gross-unwithheld",
                    "posting_date": "2026-04-04",
                    "evidence_references": [evidence.id],
                    "components": [{
                        "key": "labor",
                        "kind": "labor_settlement",
                        "business_date": "2026-04-04",
                        "payment_date": "2026-04-04",
                        "source_open_item_id": labor_open_item.id,
                        "amount_fen": 500_000,
                        "settlement_mode": "gross_paid_without_withholding",
                        "withholding_exception_evidence_ids": [evidence.id],
                    }],
                    "funds": [{
                        "key": "bank",
                        "account_code": "1002",
                        "direction": "payment",
                        "payment_date": "2026-04-04",
                        "amount_fen": 500_000,
                        "allocations": [{"component_key": "labor", "amount_fen": 500_000}],
                        "bank_transaction_references": [{"id": payout_bank.id}],
                    }],
                    }))
                assert payout.status == "posted", payout
                session.commit()

                derived = next(
                    item["derived"] for item in payout.data["components"]
                    if item["key"] == "labor"
                )
                assert derived["theoretical_withholding_tax_fen"] == 80_000
                assert derived["withholding_tax_fen"] == 0
                assert derived["unwithheld_tax_fen"] == 80_000
                assert payout_bank.matched_event_id == payout.event_id
                assert labor_open_item.status == "settled"
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(BankTransactionMatch)
                        .where(
                            BankTransactionMatch.bank_transaction_id == payout_bank.id,
                            BankTransactionMatch.event_id == payout.event_id,
                            BankTransactionMatch.invalidated_by_event_id.is_(None),
                        )
                    )
                    == 1
                )
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(OpenItem)
                        .where(
                            OpenItem.source_event_id == payout.event_id,
                            OpenItem.payable_category == "labor_individual_income_tax",
                        )
                    )
                    == 0
                )
        finally:
            authority_stack.close()
            engine.dispose()


def test_postgres_labor_confirmation_is_concurrent_and_direct_writes_fail_closed() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        config.attributes["database_url_override"] = database_url
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        factory = make_session_factory(engine)
        authority_stack = ExitStack()
        try:
            with factory() as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="个人劳务 PostgreSQL 硬约束测试",
                    accounting_period_control_enabled=False,
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(session, organization)
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    evidence = _evidence(session, organization.id, "a")
                with authority.attributed_call(
                    session, tool_name="finance_register_labor_service_person"
                ):
                    registered = LaborRemunerationService(session).register_person(
                        RegisterLaborServicePersonRequest(
                        org_id=organization.id,
                        idempotency_key="pg-labor-person",
                        person_code="PG-L001",
                        name="PostgreSQL 劳务人员",
                        relationship_start_date=date(2026, 7, 1),
                        status="active",
                        evidence_references=[evidence.id],
                        )
                    )
                with authority.attributed_call(
                    session, tool_name="finance_preview_labor_remuneration_batch"
                ):
                    preview = LaborRemunerationService(session).preview_batch(
                        PreviewLaborRemunerationBatchRequest(
                        org_id=organization.id,
                        idempotency_key="pg-labor-preview",
                        remuneration_period="2026-07",
                        business_date=date(2026, 7, 31),
                        posting_date=date(2026, 7, 31),
                        planned_payment_date=date(2026, 8, 5),
                        items=[
                            LaborRemunerationItemFacts(
                                labor_person_id=registered.labor_person_id,
                                service_start_date=date(2026, 7, 1),
                                service_end_date=date(2026, 7, 31),
                                fixed_fee_fen=300_000,
                                commission_fen=200_000,
                                expense_role="labor_service_cost",
                                tax_identity="resident",
                                income_grouping="continuous_monthly",
                                is_full_time_student=False,
                                external_declaration_status="not_due",
                            )
                        ],
                        evidence_references=[evidence.id],
                        )
                    )
                org_id = organization.id
                batch_id = preview.batch_id
                calculation_hash = preview.calculation_hash
                session.commit()

            assert batch_id is not None and calculation_hash is not None
            barrier = Barrier(2)

            def confirm() -> object:
                barrier.wait(timeout=10)
                with factory.begin() as session:
                    with authority.attributed_call(
                        session, tool_name="finance_confirm_labor_remuneration_batch"
                    ):
                        return LaborRemunerationService(session).confirm_batch(
                            ConfirmLaborRemunerationBatchRequest(
                                org_id=org_id,
                                batch_id=batch_id,
                                idempotency_key="pg-labor-concurrent-confirm",
                                calculation_hash=calculation_hash,
                                confirmation_note="并发确认必须收敛为同一正式事件",
                            )
                        )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: confirm(), range(2)))
            assert {result.status.value for result in results} == {"posted"}
            assert len({result.event_id for result in results}) == 1

            with factory.begin() as session:
                line = session.scalar(
                    select(LaborRemunerationLine).where(
                        LaborRemunerationLine.batch_id == batch_id
                        )
                    )
                assert line is not None
                with authority.attributed_call(session, tool_name="finance_negative_tamper"):
                    with pytest.raises(DBAPIError):
                        with session.begin_nested():
                            session.execute(
                                update(LaborRemunerationLine)
                                .where(LaborRemunerationLine.id == line.id)
                                .values(commission_fen=line.commission_fen + 1)
                            )
                            session.flush()

                forged_bank = BankTransaction(
                    org_id=org_id,
                    bank_account_code="1002",
                    fingerprint="3" * 64,
                    external_id="pg-forged-labor-bank",
                    booking_date=date(2026, 8, 5),
                    amount_fen=-420_000,
                    memo="绕过受控导入",
                    source_sha256="4" * 64,
                )
                with authority.attributed_call(session, tool_name="finance_negative_tamper"):
                    with pytest.raises((DBAPIError, IntegrityError)):
                        with session.begin_nested():
                            session.add(forged_bank)
                            session.flush()
        finally:
            authority_stack.close()
            engine.dispose()
