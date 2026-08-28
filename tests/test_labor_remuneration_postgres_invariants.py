from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from alembic.config import Config
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.labor_remuneration_schemas import (
    ConfirmLaborRemunerationBatchRequest,
    ConfirmUnifiedPayoutRunRequest,
    LaborPayoutItem,
    LaborRemunerationItemFacts,
    PreviewLaborRemunerationBatchRequest,
    PreviewUnifiedPayoutRunRequest,
    RegisterLaborServicePersonRequest,
)
from ai_accounting.labor_remuneration_service import LaborRemunerationService
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    Evidence,
    LaborRemunerationLine,
    LaborServicePersonEvidence,
    OpenItem,
    UnifiedPayoutRunItem,
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


def test_postgres_formal_baseline_installs_labor_invariants() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        try:
            columns = {
                column["name"] for column in inspect(engine).get_columns("unified_payout_run_items")
            }
            assert {
                "settlement_mode",
                "theoretical_individual_income_tax_fen",
                "unwithheld_individual_income_tax_fen",
            } <= columns
            checks = {
                constraint["name"]
                for constraint in inspect(engine).get_check_constraints(
                    "unified_payout_run_items"
                )
            }
            assert "ck_payout_item_settlement_mode" in checks
            with engine.connect() as connection:
                definition = connection.scalar(
                    text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_unified_payout_0013(uuid)'::regprocedure)"
                    )
                )
                assert "theoretical_individual_income_tax_fen" in definition
                assert "UNIFIED_PAYOUT_WITHHOLDING_EXCEPTION_EVIDENCE_MISMATCH" in definition
                final_event_wrapper = connection.scalar(
                    text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_final_business_event_0014(uuid)'::regprocedure)"
                    )
                )
                assert "labor_remuneration_accrual" in final_event_wrapper
                assert "unified_payout_run" in final_event_wrapper
                assert "labor_withholding_tax_payment" in final_event_wrapper

        finally:
            engine.dispose()


def test_postgres_labor_accrual_and_gross_unwithheld_payout_commit_end_to_end() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        factory = make_session_factory(engine)
        try:
            with factory() as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="个人劳务 PostgreSQL 终态事件端到端测试",
                    accounting_period_control_enabled=True,
                )
                prepare_authenticated_bank_account(
                    session,
                    organization,
                    booking_date=date(2026, 3, 31),
                )
                prepare_authenticated_bank_account(
                    session,
                    organization,
                    booking_date=date(2026, 4, 4),
                )
                evidence = _evidence(session, organization.id, "b")
                service = LaborRemunerationService(session)
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
                payout_bank = import_test_bank_transaction(
                    session,
                    organization,
                    amount_fen=-500_000,
                    key="pg-gross-unwithheld-bank",
                    booking_date=date(2026, 4, 4),
                )
                payout_preview = service.preview_payout(
                    PreviewUnifiedPayoutRunRequest(
                        org_id=organization.id,
                        idempotency_key="pg-gross-unwithheld-preview",
                        business_date=date(2026, 4, 4),
                        payment_date=date(2026, 4, 4),
                        posting_date=date(2026, 4, 4),
                        bank_account_code="1002",
                        bank_transaction_id=payout_bank.id,
                        labor_items=[
                            LaborPayoutItem(
                                source_open_item_id=labor_open_item.id,
                                settlement_mode="gross_paid_without_withholding",
                            )
                        ],
                        evidence_references=[evidence.id],
                        withholding_exception_evidence_references=[evidence.id],
                    )
                )
                assert payout_preview.status.value == "calculated"
                assert payout_preview.data["theoretical_individual_income_tax_total_fen"] == 80_000
                assert payout_preview.data["unwithheld_individual_income_tax_total_fen"] == 80_000
                assert payout_preview.data["withholding_total_fen"] == 0
                payout = service.confirm_payout(
                    ConfirmUnifiedPayoutRunRequest(
                        org_id=organization.id,
                        payout_run_id=payout_preview.payout_run_id,
                        idempotency_key="pg-gross-unwithheld-confirm",
                        calculation_hash=payout_preview.calculation_hash,
                        confirmation_note="确认 PostgreSQL 毛额已付且实际未扣税",
                    )
                )
                assert payout.status.value == "posted"
                session.commit()

                run_item = session.scalar(
                    select(UnifiedPayoutRunItem).where(
                        UnifiedPayoutRunItem.payout_run_id == payout.payout_run_id
                    )
                )
                assert run_item is not None
                assert run_item.theoretical_individual_income_tax_fen == 80_000
                assert run_item.individual_income_tax_fen == 0
                assert run_item.unwithheld_individual_income_tax_fen == 80_000
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
            engine.dispose()


def test_postgres_labor_confirmation_is_concurrent_and_direct_writes_fail_closed() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        factory = make_session_factory(engine)
        try:
            with factory.begin() as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="个人劳务 PostgreSQL 硬约束测试",
                    accounting_period_control_enabled=False,
                )
                evidence = _evidence(session, organization.id, "a")
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
                evidence_id = evidence.id
                batch_id = preview.batch_id
                calculation_hash = preview.calculation_hash
                person_id = registered.labor_person_id

            assert batch_id is not None and calculation_hash is not None
            barrier = Barrier(2)

            def confirm() -> object:
                barrier.wait(timeout=10)
                with factory.begin() as session:
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
                with pytest.raises((DBAPIError, IntegrityError)):
                    with session.begin_nested():
                        session.add(forged_bank)
                        session.flush()

                other = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="个人劳务跨组织硬约束测试",
                    accounting_period_control_enabled=False,
                )
                with pytest.raises((DBAPIError, IntegrityError)):
                    with session.begin_nested():
                        session.add(
                            LaborServicePersonEvidence(
                                org_id=other.id,
                                labor_person_id=person_id,
                                evidence_id=evidence_id,
                            )
                        )
                        session.flush()
                        session.connection().exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
        finally:
            engine.dispose()
