from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
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
    Evidence,
    LaborRemunerationLine,
    LaborServicePersonEvidence,
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


def test_postgres_gross_unwithheld_migration_installs_and_restores_invariants() -> None:
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

            command.downgrade(config, "0013_labor_remuneration")
            downgraded_columns = {
                column["name"] for column in inspect(engine).get_columns("unified_payout_run_items")
            }
            assert "settlement_mode" not in downgraded_columns
            with engine.connect() as connection:
                restored = connection.scalar(
                    text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_unified_payout_0013(uuid)'::regprocedure)"
                    )
                )
                assert "theoretical_individual_income_tax_fen" not in restored
                assert "UNIFIED_PAYOUT_WITHHOLDING_EXCEPTION_EVIDENCE_MISMATCH" not in restored
            command.upgrade(config, "head")
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
                    name="个人劳务 PostgreSQL 硬约束测试",
                    accounting_period_control_enabled=False,
                )
                evidence = _evidence(session, organization.id, "p")
                registered = LaborRemunerationService(session).register_person(
                    RegisterLaborServicePersonRequest(
                        org_id=organization.id,
                        idempotency_key="pg-labor-person",
                        person_code="PG-L001",
                        name="PostgreSQL 劳务人员",
                        relationship_start_date=date(2026, 8, 1),
                        status="active",
                        evidence_references=[evidence.id],
                    )
                )
                preview = LaborRemunerationService(session).preview_batch(
                    PreviewLaborRemunerationBatchRequest(
                        org_id=organization.id,
                        idempotency_key="pg-labor-preview",
                        remuneration_period="2026-08",
                        business_date=date(2026, 8, 31),
                        posting_date=date(2026, 8, 31),
                        planned_payment_date=date(2026, 9, 5),
                        items=[
                            LaborRemunerationItemFacts(
                                labor_person_id=registered.labor_person_id,
                                service_start_date=date(2026, 8, 1),
                                service_end_date=date(2026, 8, 31),
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
                    booking_date=date(2026, 9, 5),
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
