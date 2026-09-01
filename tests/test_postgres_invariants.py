from __future__ import annotations

import shutil
from datetime import date

import pytest
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import get_account_by_role, seed_organization
from ai_accounting.models import BusinessEvent, Voucher, VoucherLine
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def test_postgres_rejects_unbalanced_and_mutated_posted_vouchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", url)
        alembic_config = Config("alembic.ini")
        alembic_config.attributes["database_url_override"] = url
        command.upgrade(alembic_config, "head")
        from sqlalchemy import create_engine

        engine = create_engine(url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="PostgreSQL 约束测试",
                )
                event = BusinessEvent(
                    org_id=organization.id,
                    idempotency_key="unbalanced-direct-write",
                    event_type="expense_payable",
                    status="draft",
                    description="应在提交时失败",
                    facts={},
                    business_date=date(2026, 8, 8),
                    posting_date=date(2026, 8, 8),
                    rule_trace=[],
                )
                session.add(event)
                session.flush()
                voucher = Voucher(
                    org_id=organization.id,
                    event_id=event.id,
                    voucher_number="202608-9998",
                    posting_date=date(2026, 8, 8),
                    description="不平凭证",
                    status="draft",
                )
                session.add(voucher)
                session.flush()
                bank = get_account_by_role(session, organization.id, "bank")
                session.add(
                    VoucherLine(
                        org_id=organization.id,
                        voucher_id=voucher.id,
                        line_number=1,
                        account_id=bank.id,
                        debit_fen=100,
                        credit_fen=0,
                    )
                )
                voucher.status = "posted"
                with pytest.raises(DBAPIError):
                    session.commit()

            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="不可变约束测试",
                )
                event = BusinessEvent(
                    org_id=organization.id,
                    idempotency_key="balanced-direct-write",
                    event_type="expense_payable",
                    status="draft",
                    description="合法凭证",
                    facts={},
                    business_date=date(2026, 8, 8),
                    posting_date=date(2026, 8, 8),
                    rule_trace=[],
                )
                session.add(event)
                session.flush()
                voucher = Voucher(
                    org_id=organization.id,
                    event_id=event.id,
                    voucher_number="202608-9999",
                    posting_date=date(2026, 8, 8),
                    description="合法凭证",
                    status="draft",
                )
                session.add(voucher)
                session.flush()
                bank = get_account_by_role(session, organization.id, "bank")
                revenue = get_account_by_role(session, organization.id, "service_revenue")
                session.add_all(
                    [
                        VoucherLine(
                            org_id=organization.id,
                            voucher_id=voucher.id,
                            line_number=1,
                            account_id=bank.id,
                            debit_fen=100,
                            credit_fen=0,
                        ),
                        VoucherLine(
                            org_id=organization.id,
                            voucher_id=voucher.id,
                            line_number=2,
                            account_id=revenue.id,
                            debit_fen=0,
                            credit_fen=100,
                        ),
                    ]
                )
                session.flush()
                voucher.status = "posted"
                event.status = "posted"
                session.commit()
                voucher.description = "禁止修改"
                with pytest.raises(DBAPIError):
                    session.commit()
        finally:
            engine.dispose()
