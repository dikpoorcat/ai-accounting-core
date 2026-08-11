from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import BankTransaction, BankTransactionMatch, BusinessEvent
from ai_accounting.schemas import RecordEventRequest, ReverseEventRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _bank_transaction(org_id: object, *, amount_fen: int, key: str) -> BankTransaction:
    return BankTransaction(
        org_id=org_id,
        bank_account_code="1002",
        fingerprint=(key * 64)[:64],
        booking_date=date(2026, 3, 5),
        amount_fen=amount_fen,
        currency="CNY",
        memo=key,
        source_sha256=("source-" + key * 64)[:64],
    )


def _expense_request(org_id: object, bank_id: object, *, key: str) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "event_type": "expense_cash",
            "business_dates": {
                "business_date": "2026-03-05",
                "payment_date": "2026-03-05",
                "posting_date": "2026-03-05",
            },
            "amounts": {
                "gross_amount_fen": 100,
                "expense_account_role": "general_expense",
            },
            "bank_transaction_references": [{"id": bank_id}],
        }
    )


def _reverse_request(org_id: object, event_id: object, *, key: str) -> ReverseEventRequest:
    return ReverseEventRequest(
        org_id=org_id,
        event_id=event_id,
        idempotency_key=key,
        reason="R3 并发冲正",
        posting_date=date(2026, 3, 6),
    )


def test_r3_008_reversal_replays_after_source_lock_and_r3_009_rebooks_bank_row() -> None:
    """Two PG sessions replay the same reversal and retain immutable bank history."""

    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        factory = make_session_factory(engine)
        try:
            with factory.begin() as session:
                organization = seed_organization(
                    session, accounting_period_control_enabled=False, name="R3 幂等与银行匹配企业"
                )
                bank = _bank_transaction(organization.id, amount_fen=-100, key="r3-bank-rebook")
                session.add(bank)
                session.flush()
                posted = FinanceService(session).record_event(
                    _expense_request(organization.id, bank.id, key="r3-original-expense")
                )
                assert posted.status == "posted", posted.errors
                org_id = organization.id
                bank_id = bank.id
                original_event_id = posted.event_id

            request = _reverse_request(
                org_id,
                original_event_id,
                key="r3-concurrent-reversal",
            )
            barrier = Barrier(2)

            def reverse_same_payload() -> object:
                barrier.wait(timeout=10)
                with factory.begin() as session:
                    return FinanceService(session).reverse_event(request)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: reverse_same_payload(), range(2)))

            assert {result.status for result in results} == {"posted"}
            assert len({result.event_id for result in results}) == 1
            reversal_event_id = results[0].event_id

            with factory.begin() as session:
                rebooked = FinanceService(session).record_event(
                    _expense_request(org_id, bank_id, key="r3-corrected-expense")
                )
                assert rebooked.status == "posted", rebooked.errors
                rebooked_event_id = rebooked.event_id

            with factory() as session:
                bank = session.get(BankTransaction, bank_id)
                assert bank is not None and bank.matched_event_id == rebooked_event_id
                matches = session.scalars(
                    select(BankTransactionMatch)
                    .where(BankTransactionMatch.org_id == org_id)
                    .order_by(BankTransactionMatch.created_at, BankTransactionMatch.id)
                ).all()
                assert len(matches) == 2
                original_match = next(
                    match for match in matches if match.event_id == original_event_id
                )
                current_match = next(
                    match for match in matches if match.event_id == rebooked_event_id
                )
                assert original_match.invalidated_by_event_id == reversal_event_id
                assert original_match.invalidated_at is not None
                assert current_match.invalidated_by_event_id is None
                original = session.get(BusinessEvent, original_event_id)
                assert original is not None and original.status == "reversed"

            different_keys = [
                _reverse_request(org_id, rebooked_event_id, key="r3-reversal-race-a"),
                _reverse_request(org_id, rebooked_event_id, key="r3-reversal-race-b"),
            ]
            different_key_barrier = Barrier(2)

            def reverse_with_different_key(request: ReverseEventRequest) -> object:
                different_key_barrier.wait(timeout=10)
                with factory.begin() as session:
                    return FinanceService(session).reverse_event(request)

            with ThreadPoolExecutor(max_workers=2) as executor:
                contended_results = list(executor.map(reverse_with_different_key, different_keys))

            assert {result.status for result in contended_results} == {"posted", "rejected"}
            rejected = next(result for result in contended_results if result.status == "rejected")
            assert rejected.errors == ["EVENT_IS_NOT_REVERSIBLE"]
        finally:
            engine.dispose()
