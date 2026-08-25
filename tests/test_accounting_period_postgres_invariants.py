from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.accounting_periods import (
    ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM,
    ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
    ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS,
    canonical_sha256,
)
from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    Evidence,
    ExecutionAttribution,
    Organization,
)
from ai_accounting.schemas import RecordEventRequest, ReverseEventRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _approve_close(
    session: Session,
    attribution: ExecutionAttribution,
    *,
    period_id: uuid.UUID,
    calculation_hash: str,
) -> uuid.UUID:
    now = datetime.now(UTC)
    approval = AccountingPeriodCloseApproval(
        org_id=attribution.org_id,
        period_id=period_id,
        owner_account_id=attribution.owner_account_id,
        owner_session_id=attribution.owner_session_id,
        owner_credential_version=attribution.owner_credential_version,
        calculation_hash=calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(approval)
    session.flush()
    return approval.id


def _sale(
    org_id: object,
    key: str,
    *,
    business_date: date = date(2026, 7, 15),
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "event_type": "service_credit_sale",
            "counterparty": {"kind": "customer", "name": "期间测试客户"},
            "business_dates": {
                "business_date": business_date,
                "posting_date": business_date,
                "fulfillment_date": business_date,
                "payment_date": business_date,
                "tax_obligation_date": business_date,
            },
            "amounts": {"gross_amount_fen": 101_000},
            "tax_facts": {
                "taxable": True,
                "rate_percent": "1",
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "tax_due_on_event": True,
            },
        }
    )


def _insert_raw_event(
    connection: sa.Connection,
    *,
    org_id: object,
    posting_date: date,
    status: str,
    key: str,
    event_type: str = "service_cash_sale",
    facts: dict[str, object] | None = None,
    event_id: uuid.UUID | None = None,
    execution_attribution_id: uuid.UUID | None = None,
) -> uuid.UUID:
    event_id = event_id or uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO business_events (
                id, org_id, idempotency_key, request_payload_hash,
                event_type, status, description, facts, business_date,
                fulfillment_date, invoice_date, payment_date,
                tax_obligation_date, posting_date, rule_trace,
                rule_version, reversed_by_event_id, execution_attribution_id, created_at
            ) VALUES (
                :event_id, :org_id, :key, :hash,
                :event_type, :status, '', CAST(:facts AS jsonb), :posting_date,
                NULL, NULL, NULL, NULL, :posting_date, '[]'::jsonb,
                NULL, NULL, :execution_attribution_id, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "event_id": event_id,
            "org_id": org_id,
            "key": key,
            "hash": "d" * 64,
            "event_type": event_type,
            "status": status,
            "facts": json.dumps(facts or {}),
            "posting_date": posting_date,
            "execution_attribution_id": execution_attribution_id,
        },
    )
    return event_id


def _insert_raw_payroll_batch(
    connection: sa.Connection,
    *,
    org_id: object,
    policy_id: object,
    posting_date: date,
    key: str,
    status: str = "calculated",
    version: int = 1,
    execution_attribution_id: uuid.UUID | None = None,
) -> uuid.UUID:
    batch_id = uuid.uuid4()
    connection.execute(
        sa.text(
            """
            INSERT INTO payroll_batches (
                id, org_id, idempotency_key, batch_kind, payroll_period,
                version, status, calculation_hash, request_payload_hash,
                calculation_input, calculation_trace, policy_snapshot,
                policy_version_id, posting_date, payment_date, tax_method,
                confirmed_by, confirmation_note, confirmed_at,
                business_event_id, reversal_of_batch_id, execution_attribution_id, created_at
            ) VALUES (
                :id, :org_id, :key, 'regular', :payroll_period,
                :version, :status, :hash, NULL, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb,
                :policy_id, :posting_date, :posting_date, NULL,
                NULL, NULL, NULL, NULL, NULL, :execution_attribution_id, CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "id": batch_id,
            "org_id": org_id,
            "key": key,
            "payroll_period": posting_date.strftime("%Y-%m"),
            "status": status,
            "version": version,
            "hash": uuid.uuid5(uuid.NAMESPACE_URL, key).hex * 2,
            "policy_id": policy_id,
            "posting_date": posting_date,
            "execution_attribution_id": execution_attribution_id,
        },
    )
    return batch_id


def test_postgres_period_close_snapshot_and_direct_sql_guards(
    authenticated_zero_bank_scope: object,
) -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        config = _config(database_url)
        command.upgrade(config, "head")
        command.check(config)
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG期间提交点",
                )
                session.flush()
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="7" * 64,
                    original_name="period.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/period.txt",
                )
                session.add(evidence)
                session.flush()
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            policy_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO payroll_policy_versions (
                            id, org_id, region, supersedes_id, effective_from,
                            effective_to, version, source_url, parameters, created_at
                        ) VALUES (
                            :id, :org_id, '期间门禁测试', NULL, '2026-01-01',
                            '2026-12-31', 'period-guard-v1',
                            'https://www.chinatax.gov.cn/', '{}'::jsonb,
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": policy_id, "org_id": org_id},
                )

            with Session(engine) as session:
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                generated = period_service.generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-07",
                        idempotency_key="pg-generate-july",
                        confirmation_note="PG逐月生成",
                        evidence_references=[evidence_id],
                    )
                )
                assert generated.status == "posted", generated.errors
                session.commit()
                period_id = generated.period_id

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_NOT_GENERATED"):
                with engine.begin() as connection:
                    _insert_raw_event(
                        connection,
                        org_id=org_id,
                        posting_date=date(2026, 6, 30),
                        status="draft",
                        key="direct-draft-before-start",
                    )
            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_NOT_GENERATED"):
                with engine.begin() as connection:
                    _insert_raw_payroll_batch(
                        connection,
                        org_id=org_id,
                        policy_id=policy_id,
                        posting_date=date(2026, 6, 30),
                        key="direct-payroll-before-start",
                    )

            with engine.begin() as connection:
                superseded_id = _insert_raw_payroll_batch(
                    connection,
                    org_id=org_id,
                    policy_id=policy_id,
                    posting_date=date(2026, 7, 20),
                    key="direct-payroll-open-superseded",
                )
                connection.execute(
                    sa.text("UPDATE payroll_batches SET status = 'superseded' WHERE id = :id"),
                    {"id": superseded_id},
                )

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_NOT_GENERATED"):
                with engine.begin() as connection:
                    event_id, voucher_id = uuid.uuid4(), uuid.uuid4()
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO business_events (
                                id, org_id, idempotency_key, request_payload_hash,
                                event_type, status, description, facts, business_date,
                                fulfillment_date, invoice_date, payment_date,
                                tax_obligation_date, posting_date, rule_trace,
                                rule_version, reversed_by_event_id, created_at
                            ) VALUES (
                                :event_id, :org_id, 'direct-before-start', :hash,
                                'service_cash_sale', 'draft', '', '{}'::jsonb,
                                '2026-06-30', NULL, NULL, NULL, NULL, '2026-06-30',
                                '[]'::jsonb, NULL, NULL, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {"event_id": event_id, "org_id": org_id, "hash": "a" * 64},
                    )
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO vouchers (
                                id, org_id, event_id, voucher_number, posting_date,
                                description, status, reversal_of_voucher_id, posted_at
                            ) VALUES (
                                :voucher_id, :org_id, :event_id, 'DIRECT-BEFORE',
                                '2026-06-30', '', 'posted', NULL, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {"voucher_id": voucher_id, "org_id": org_id, "event_id": event_id},
                    )

            with Session(engine) as session:
                sale = FinanceService(session).record_event(_sale(org_id, "pg-july-sale"))
                assert sale.status == "posted", sale.errors
                session.commit()
                sale_event_id = sale.event_id

            with Session(engine) as session:
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                august = period_service.generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-08",
                        idempotency_key="pg-generate-august",
                        confirmation_note="PG连续生成八月",
                        evidence_references=[evidence_id],
                    )
                )
                assert august.status == "posted", august.errors
                future_sale = FinanceService(session).record_event(
                    _sale(
                        org_id,
                        "pg-august-sale-before-july-close",
                        business_date=date(2026, 8, 1),
                    )
                )
                assert future_sale.status == "posted", future_sale.errors
                session.commit()
                august_period_id = august.period_id

            with Session(engine) as session:
                organization = session.get(Organization, org_id)
                assert organization is not None
                authority = authenticated_zero_bank_scope(
                    session,
                    organization,
                    evidence_id=evidence_id,
                )
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                preview_request = PreviewAccountingPeriodCloseRequest(
                    org_id=org_id,
                    period_id=period_id,
                    closing_date=date(2026, 7, 31),
                )
                preview = period_service.preview_accounting_period_close(preview_request)
                assert preview.data["calculation"]["review_counts"]["open_items"] == 1
                with authority.attributed_call(
                    session, tool_name="finance_confirm_accounting_period_close"
                ) as attribution:
                    owner_approval_id = _approve_close(
                        session,
                        attribution,
                        period_id=period_id,
                        calculation_hash=preview.calculation_hash,
                    )
                    confirmed = period_service.confirm_accounting_period_close(
                        ConfirmAccountingPeriodCloseRequest(
                            **preview_request.model_dump(),
                            calculation_hash=preview.calculation_hash,
                            owner_approval_id=owner_approval_id,
                            idempotency_key="pg-close-july",
                            review_facts=AccountingPeriodReviewFacts(
                                voucher_completeness_reviewed=True,
                                bank_reconciliation_reviewed=True,
                                open_items_reviewed=True,
                                payroll_and_statutory_items_reviewed=True,
                                tax_items_reviewed=True,
                                asset_and_borrowing_schedules_reviewed=True,
                            ),
                            confirmation_note="PG月结",
                            evidence_references=[evidence_id],
                        )
                    )
                assert confirmed.status == "posted", confirmed.errors
                session.commit()
                close_id = confirmed.close_id
                original_hash = confirmed.calculation_hash
                original_payload = session.get(AccountingPeriodClose, close_id).calculation_payload

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_CLOSED"):
                with engine.begin() as connection:
                    with Session(bind=connection) as attributed_session:
                        with authority.attributed_call(
                            attributed_session, tool_name="finance_record_event"
                        ) as attribution:
                            _insert_raw_event(
                                connection,
                                org_id=org_id,
                                posting_date=date(2026, 7, 20),
                                status="draft",
                                key="direct-draft-closed",
                                execution_attribution_id=attribution.id,
                            )
            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_CLOSED"):
                with engine.begin() as connection:
                    with Session(bind=connection) as attributed_session:
                        with authority.attributed_call(
                            attributed_session, tool_name="finance_record_event"
                        ) as attribution:
                            audit_event_id = _insert_raw_event(
                                connection,
                                org_id=org_id,
                                posting_date=date(2026, 7, 20),
                                status="needs_information",
                                key="direct-draft-voucher-event",
                                execution_attribution_id=attribution.id,
                            )
                            connection.execute(
                                sa.text(
                                    """
                                    INSERT INTO vouchers (
                                        id, org_id, event_id, voucher_number, posting_date,
                                        description, status, reversal_of_voucher_id, posted_at
                                    ) VALUES (
                                        :id, :org_id, :event_id, 'DIRECT-DRAFT-CLOSED',
                                        '2026-07-20', '', 'draft', NULL, CURRENT_TIMESTAMP
                                    )
                                    """
                                ),
                                {
                                    "id": uuid.uuid4(),
                                    "org_id": org_id,
                                    "event_id": audit_event_id,
                                },
                            )
            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_CLOSED"):
                with engine.begin() as connection:
                    with Session(bind=connection) as attributed_session:
                        with authority.attributed_call(
                            attributed_session, tool_name="finance_confirm_payroll"
                        ) as attribution:
                            _insert_raw_payroll_batch(
                                connection,
                                org_id=org_id,
                                policy_id=policy_id,
                                posting_date=date(2026, 7, 20),
                                key="direct-payroll-closed",
                                version=2,
                                execution_attribution_id=attribution.id,
                            )
            with engine.connect() as connection:
                future_posting_date = connection.scalar(
                    sa.text("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date")
                ) + timedelta(days=1)
            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"):
                with engine.begin() as connection:
                    with Session(bind=connection) as attributed_session:
                        with authority.attributed_call(
                            attributed_session, tool_name="finance_record_event"
                        ) as attribution:
                            _insert_raw_event(
                                connection,
                                org_id=org_id,
                                posting_date=future_posting_date,
                                status="draft",
                                key="direct-draft-future",
                                execution_attribution_id=attribution.id,
                            )
            with engine.begin() as connection:
                with Session(bind=connection) as attributed_session:
                    with authority.attributed_call(
                        attributed_session, tool_name="finance_record_event"
                    ) as attribution:
                        _insert_raw_event(
                            connection,
                            org_id=org_id,
                            posting_date=future_posting_date,
                            status="needs_information",
                            key="audit-needs-information-future",
                            execution_attribution_id=attribution.id,
                        )
                        _insert_raw_event(
                            connection,
                            org_id=org_id,
                            posting_date=future_posting_date,
                            status="rejected",
                            key="audit-rejected-future",
                            execution_attribution_id=attribution.id,
                        )

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "UPDATE accounting_period_closes "
                            "SET calculation_hash = repeat('0', 64) WHERE id = :id"
                        ),
                        {"id": close_id},
                    )
            with Session(engine) as session:
                close = session.get(AccountingPeriodClose, close_id)
                assert close.voucher_count == 1
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    rejected = FinanceService(session).record_event(_sale(org_id, "after-close"))
                assert rejected.errors == ["ACCOUNTING_PERIOD_CLOSED"]

            with Session(engine) as session:
                august = session.get(AccountingPeriod, august_period_id)
                assert august is not None and august.status == "open"
            with Session(engine) as session:
                with authority.attributed_call(session, tool_name="finance_reverse_event"):
                    reversal = FinanceService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=sale_event_id,
                            idempotency_key="pg-reverse-july-in-august",
                            reason="后续开放月更正",
                            posting_date=date(2026, 8, 1),
                        )
                    )
                assert reversal.status == "posted", reversal.errors
                session.commit()
                close = session.get(AccountingPeriodClose, close_id)
                assert close.calculation_hash == original_hash
                assert close.calculation_payload == original_payload
        finally:
            engine.dispose()


def test_postgres_close_vs_close_is_linearized(
    authenticated_zero_bank_scope: object,
) -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG并发月结",
                )
                session.flush()
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="8" * 64,
                    original_name="concurrent.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/concurrent.txt",
                )
                session.add(evidence)
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            with Session(engine) as session:
                generated = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-07",
                        idempotency_key="pg-concurrent-generate",
                        confirmation_note="并发测试空月生成",
                        evidence_references=[evidence_id],
                    )
                )
                session.commit()
                period_id = generated.period_id
            preview_request = PreviewAccountingPeriodCloseRequest(
                org_id=org_id,
                period_id=period_id,
                closing_date=date(2026, 7, 31),
            )
            with Session(engine) as session:
                organization = session.get(Organization, org_id)
                assert organization is not None
                authority = authenticated_zero_bank_scope(
                    session,
                    organization,
                    evidence_id=evidence_id,
                    executor_name="postgres-close-linearization",
                )
                session.commit()
            with Session(engine) as session:
                preview = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).preview_accounting_period_close(preview_request)

            barrier = Barrier(2)

            def close(key: str) -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    with authority.attributed_call(
                        session, tool_name="finance_confirm_accounting_period_close"
                    ) as attribution:
                        owner_approval_id = _approve_close(
                            session,
                            attribution,
                            period_id=period_id,
                            calculation_hash=preview.calculation_hash,
                        )
                        result = AccountingPeriodService(
                            session, current_date=date(2026, 8, 11)
                        ).confirm_accounting_period_close(
                            ConfirmAccountingPeriodCloseRequest(
                                **preview_request.model_dump(),
                                calculation_hash=preview.calculation_hash,
                                owner_approval_id=owner_approval_id,
                                idempotency_key=key,
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    tax_items_reviewed=True,
                                    asset_and_borrowing_schedules_reviewed=True,
                                ),
                                confirmation_note="显式确认空月无业务并关闭",
                                evidence_references=[evidence_id],
                            )
                        )
                    session.commit()
                    return str(result.status), result.errors

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(close, ("pg-close-first", "pg-close-second")))
            assert sorted(status for status, _errors in results) == ["posted", "rejected"]
            rejected_errors = next(errors for status, errors in results if status == "rejected")
            assert rejected_errors == ["ACCOUNTING_PERIOD_ALREADY_CLOSED"]
            with engine.connect() as connection:
                assert (
                    connection.scalar(sa.text("SELECT count(*) FROM accounting_period_closes")) == 1
                )
        finally:
            engine.dispose()


def test_postgres_close_vs_post_is_linearized(
    authenticated_zero_bank_scope: object,
) -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG入账月结并发",
                )
                session.flush()
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="9" * 64,
                    original_name="post-close.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/post-close.txt",
                )
                session.add(evidence)
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            with Session(engine) as session:
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                generated = period_service.generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-07",
                        idempotency_key="pg-post-close-generate",
                        confirmation_note="入账月结并发期间",
                        evidence_references=[evidence_id],
                    )
                )
                session.commit()
                period_id = generated.period_id
            with Session(engine) as session:
                baseline = FinanceService(session).record_event(
                    _sale(org_id, "pg-post-close-baseline")
                )
                assert baseline.status == "posted"
                session.commit()
            with Session(engine) as session:
                organization = session.get(Organization, org_id)
                assert organization is not None
                authority = authenticated_zero_bank_scope(
                    session,
                    organization,
                    evidence_id=evidence_id,
                    executor_name="postgres-close-post-linearization",
                )
                session.commit()
            preview_request = PreviewAccountingPeriodCloseRequest(
                org_id=org_id,
                period_id=period_id,
                closing_date=date(2026, 7, 31),
            )
            with Session(engine) as session:
                preview = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).preview_accounting_period_close(preview_request)
            barrier = Barrier(2)

            def close() -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    with authority.attributed_call(
                        session, tool_name="finance_confirm_accounting_period_close"
                    ) as attribution:
                        owner_approval_id = _approve_close(
                            session,
                            attribution,
                            period_id=period_id,
                            calculation_hash=preview.calculation_hash,
                        )
                        result = AccountingPeriodService(
                            session, current_date=date(2026, 8, 11)
                        ).confirm_accounting_period_close(
                            ConfirmAccountingPeriodCloseRequest(
                                **preview_request.model_dump(),
                                calculation_hash=preview.calculation_hash,
                                owner_approval_id=owner_approval_id,
                                idempotency_key="pg-post-close-close",
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    tax_items_reviewed=True,
                                    asset_and_borrowing_schedules_reviewed=True,
                                ),
                                confirmation_note="并发关闭",
                                evidence_references=[evidence_id],
                            )
                        )
                    session.commit()
                    return str(result.status), result.errors

            def post() -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    with authority.attributed_call(session, tool_name="finance_record_event"):
                        result = FinanceService(session).record_event(
                            _sale(org_id, "pg-post-close-racing-post")
                        )
                    session.commit()
                    return str(result.status), result.errors

            with ThreadPoolExecutor(max_workers=2) as executor:
                close_future = executor.submit(close)
                post_future = executor.submit(post)
                close_result, post_result = close_future.result(), post_future.result()
            assert [close_result[0], post_result[0]].count("posted") == 1
            if close_result[0] == "posted":
                assert post_result[1] == ["ACCOUNTING_PERIOD_CLOSED"]
            else:
                assert post_result[0] == "posted"
                assert close_result[1] == ["ACCOUNTING_PERIOD_CALCULATION_STALE"]
        finally:
            engine.dispose()


def test_postgres_period_generation_concurrency_and_payload_identity() -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG期间生成并发",
                )
                session.flush()
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="b" * 64,
                    original_name="generation.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/generation.txt",
                )
                session.add(evidence)
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            barrier = Barrier(2)

            def generate() -> tuple[str, object, object, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    result = AccountingPeriodService(
                        session, current_date=date(2026, 8, 11)
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-03",
                            idempotency_key="pg-generation-same-key",
                            confirmation_note="并发同载荷生成",
                            evidence_references=[evidence_id],
                        )
                    )
                    session.commit()
                    return (
                        str(result.status),
                        result.action_id,
                        result.period_id,
                        result.errors,
                    )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: generate(), range(2)))
            assert [row[0] for row in results] == ["posted", "posted"]
            assert len({row[1] for row in results}) == 1
            assert len({row[2] for row in results}) == 1
            with Session(engine) as session:
                service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                mismatch = service.generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-04",
                        idempotency_key="pg-generation-same-key",
                        confirmation_note="并发同载荷生成",
                        evidence_references=[evidence_id],
                    )
                )
                duplicate_month = service.generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-03",
                        idempotency_key="pg-generation-different-key",
                        confirmation_note="不同键重复月份",
                        evidence_references=[evidence_id],
                    )
                )
                session.commit()
                assert mismatch.errors == ["ACCOUNTING_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"]
                assert duplicate_month.errors == ["ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE"]
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT count(*) FROM accounting_periods")) == 1
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM accounting_period_actions WHERE status = 'posted'"
                        )
                    )
                    == 1
                )

            tamper_org_id = None
            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE"):
                with Session(engine) as session:
                    tamper_org = seed_organization(
                        session,
                        taxpayer_identification_number="91330106MA1234567T",
                        name="PG构造期篡改",
                    )
                    session.flush()
                    tamper_org_id = tamper_org.id
                    tamper_evidence = Evidence(
                        org_id=tamper_org.id,
                        sha256="c" * 64,
                        original_name="tamper.txt",
                        media_type="text/plain",
                        source="test",
                        size_bytes=1,
                        storage_path="test/tamper.txt",
                    )
                    session.add(tamper_evidence)
                    session.flush()
                    generated = AccountingPeriodService(
                        session, current_date=date(2026, 8, 11)
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=tamper_org.id,
                            period_month="2026-03",
                            idempotency_key="pg-generation-tamper",
                            confirmation_note="构造期注入额外键",
                            evidence_references=[tamper_evidence.id],
                        )
                    )
                    session.execute(
                        sa.text(
                            "UPDATE accounting_period_actions "
                            "SET input_facts = input_facts::jsonb || "
                            '\'{"secret":"forbidden"}\'::jsonb WHERE id = :id'
                        ),
                        {"id": generated.action_id},
                    )
                    session.commit()
            if tamper_org_id is not None:
                with engine.connect() as connection:
                    assert (
                        connection.scalar(
                            sa.text(
                                "SELECT count(*) FROM accounting_period_actions "
                                "WHERE org_id = :org_id"
                            ),
                            {"org_id": tamper_org_id},
                        )
                        == 0
                    )

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO accounting_period_calendars (
                                id, org_id, calendar_year, rule_version,
                                rule_effective_from, source_urls, created_at
                            ) VALUES (
                                :id, :org_id, 2027, 'forged', '2026-08-11',
                                '[]'::jsonb, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {"id": uuid.uuid4(), "org_id": org_id},
                    )
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT count(*) FROM accounting_period_calendars "
                            "WHERE org_id = :org_id AND calendar_year = 2027"
                        ),
                        {"org_id": org_id},
                    )
                    == 0
                )

            forged_action_id = uuid.uuid4()
            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO accounting_period_actions (
                                id, org_id, action_type, idempotency_key,
                                request_payload_hash, status, input_facts,
                                missing_information, errors, confirmed_by,
                                confirmation_note, created_at
                            ) VALUES (
                                :id, :org_id, 'period_generation', 'forged-failure',
                                :hash, 'rejected', '{"secret":"forbidden"}'::jsonb,
                                '["private.secret"]'::jsonb,
                                '[{"code":"RAW_SECRET","field_paths":["private.secret"]}]'::jsonb,
                                NULL, NULL, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {"id": forged_action_id, "org_id": org_id, "hash": "f" * 64},
                    )
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT count(*) FROM accounting_period_actions WHERE id = :id"),
                        {"id": forged_action_id},
                    )
                    == 0
                )

            with engine.connect() as connection:
                china_today = connection.scalar(
                    sa.text("SELECT (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date")
                )
                calendar_id = connection.scalar(
                    sa.text(
                        "SELECT id FROM accounting_period_calendars "
                        "WHERE org_id = :org_id ORDER BY calendar_year LIMIT 1"
                    ),
                    {"org_id": org_id},
                )
            future_start = (china_today.replace(day=28) + timedelta(days=4)).replace(day=1)
            following_start = (future_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            future_end = following_start - timedelta(days=1)
            for index, timezone in enumerate(("UTC", "Pacific/Honolulu"), start=1):
                with pytest.raises(
                    DBAPIError, match="ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"
                ):
                    with engine.begin() as connection:
                        connection.execute(sa.text(f"SET LOCAL TIME ZONE '{timezone}'"))
                        event_id, voucher_id = uuid.uuid4(), uuid.uuid4()
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO business_events (
                                    id, org_id, idempotency_key, request_payload_hash,
                                    event_type, status, description, facts, business_date,
                                    fulfillment_date, invoice_date, payment_date,
                                    tax_obligation_date, posting_date, rule_trace,
                                    rule_version, reversed_by_event_id, created_at
                                ) VALUES (
                                    :event_id, :org_id, :key, :hash,
                                    'service_cash_sale', 'draft', '', '{}'::jsonb,
                                    :future_date, NULL, NULL, NULL, NULL, :future_date,
                                    '[]'::jsonb, NULL, NULL, CURRENT_TIMESTAMP
                                )
                                """
                            ),
                            {
                                "event_id": event_id,
                                "org_id": org_id,
                                "key": f"future-event-{index}",
                                "hash": "1" * 64,
                                "future_date": china_today + timedelta(days=1),
                            },
                        )
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO vouchers (
                                    id, org_id, event_id, voucher_number, posting_date,
                                    description, status, reversal_of_voucher_id, posted_at
                                ) VALUES (
                                    :voucher_id, :org_id, :event_id, :number,
                                    :future_date, '', 'posted', NULL, CURRENT_TIMESTAMP
                                )
                                """
                            ),
                            {
                                "voucher_id": voucher_id,
                                "org_id": org_id,
                                "event_id": event_id,
                                "number": f"FUTURE-{index}",
                                "future_date": china_today + timedelta(days=1),
                            },
                        )
                with pytest.raises(
                    DBAPIError, match="ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED"
                ):
                    with engine.begin() as connection:
                        connection.execute(sa.text(f"SET LOCAL TIME ZONE '{timezone}'"))
                        action_id = uuid.uuid4()
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO accounting_period_actions (
                                    id, org_id, action_type, idempotency_key,
                                    request_payload_hash, status, input_facts,
                                    missing_information, errors, confirmed_by,
                                    confirmation_note, created_at
                                ) VALUES (
                                    :id, :org_id, 'period_generation', :key,
                                    :hash, 'posted', '{}'::jsonb, '[]'::jsonb,
                                    '[]'::jsonb, 'pg-reviewer', 'future', CURRENT_TIMESTAMP
                                )
                                """
                            ),
                            {
                                "id": action_id,
                                "org_id": org_id,
                                "key": f"future-period-{index}",
                                "hash": "2" * 64,
                            },
                        )
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO accounting_periods (
                                    id, org_id, calendar_id, generation_action_id,
                                    calendar_year, calendar_month, start_date, end_date,
                                    status, closed_at, close_id
                                ) VALUES (
                                    :id, :org_id, :calendar_id, :action_id,
                                    :year, :month, :start_date, :end_date,
                                    'open', NULL, NULL
                                )
                                """
                            ),
                            {
                                "id": uuid.uuid4(),
                                "org_id": org_id,
                                "calendar_id": calendar_id,
                                "action_id": action_id,
                                "year": future_start.year,
                                "month": future_start.month,
                                "start_date": future_start,
                                "end_date": future_end,
                            },
                        )
        finally:
            engine.dispose()


def test_postgres_owner_close_vs_raw_payroll_is_linearized(
    authenticated_zero_bank_scope: object,
) -> None:
    """Keep the owner-mode close race separate from legacy multi-org guards."""

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG owner月结工资并发",
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="1" * 64,
                    original_name="owner-close-payroll.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/owner-close-payroll.txt",
                )
                session.add(evidence)
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            policy_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO payroll_policy_versions (
                            id, org_id, region, supersedes_id, effective_from,
                            effective_to, version, source_url, parameters, created_at
                        ) VALUES (
                            :id, :org_id, 'owner并发门禁', NULL, '2026-01-01',
                            '2026-12-31', 'owner-concurrent-v1',
                            'https://www.chinatax.gov.cn/', '{}'::jsonb, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": policy_id, "org_id": org_id},
                )
            with Session(engine) as session:
                period = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-07",
                        idempotency_key="owner-close-payroll-generate",
                        confirmation_note="owner 并发月结期间",
                        evidence_references=[evidence_id],
                    )
                )
                assert period.status == "posted", period.errors
                session.commit()
                period_id = period.period_id
            with Session(engine) as session:
                organization = session.get(Organization, org_id)
                assert organization is not None
                authority = authenticated_zero_bank_scope(
                    session,
                    organization,
                    evidence_id=evidence_id,
                    executor_name="postgres-close-payroll-linearization",
                )
                session.commit()
            request = PreviewAccountingPeriodCloseRequest(
                org_id=org_id, period_id=period_id, closing_date=date(2026, 7, 31)
            )
            with Session(engine) as session:
                preview = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).preview_accounting_period_close(request)
            barrier = Barrier(2)

            def close() -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    with authority.attributed_call(
                        session, tool_name="finance_confirm_accounting_period_close"
                    ) as attribution:
                        owner_approval_id = _approve_close(
                            session,
                            attribution,
                            period_id=period_id,
                            calculation_hash=preview.calculation_hash,
                        )
                        result = AccountingPeriodService(
                            session, current_date=date(2026, 8, 11)
                        ).confirm_accounting_period_close(
                            ConfirmAccountingPeriodCloseRequest(
                                **request.model_dump(),
                                calculation_hash=preview.calculation_hash,
                                owner_approval_id=owner_approval_id,
                                idempotency_key="owner-close-payroll-close",
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    tax_items_reviewed=True,
                                    asset_and_borrowing_schedules_reviewed=True,
                                ),
                                confirmation_note="owner 并发月结",
                                evidence_references=[evidence_id],
                            )
                        )
                    session.commit()
                    return str(result.status), result.errors

            def payroll() -> tuple[str, str]:
                barrier.wait()
                try:
                    with engine.begin() as connection:
                        with Session(bind=connection) as session:
                            with authority.attributed_call(
                                session, tool_name="finance_confirm_payroll"
                            ) as attribution:
                                _insert_raw_payroll_batch(
                                    connection,
                                    org_id=org_id,
                                    policy_id=policy_id,
                                    posting_date=date(2026, 7, 20),
                                    key="owner-close-payroll-racing-payroll",
                                    execution_attribution_id=attribution.id,
                                )
                    return "posted", ""
                except DBAPIError as exc:
                    return "rejected", str(exc)

            with ThreadPoolExecutor(max_workers=2) as executor:
                close_future = executor.submit(close)
                payroll_future = executor.submit(payroll)
                close_status = close_future.result()
                payroll_status = payroll_future.result()
            assert [close_status[0], payroll_status[0]].count("posted") == 1
            if close_status[0] == "posted":
                assert "ACCOUNTING_PERIOD_CLOSED" in payroll_status[1]
            else:
                assert payroll_status[0] == "posted"
                assert close_status[1] == ["ACCOUNTING_PERIOD_CALCULATION_STALE"]
        finally:
            engine.dispose()


def test_postgres_payroll_dependency_and_generation_writes_are_serialized() -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG附加并发门禁",
                )
                session.flush()
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="e" * 64,
                    original_name="extra-concurrency.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/extra-concurrency.txt",
                )
                session.add(evidence)
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            policy_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO payroll_policy_versions (
                            id, org_id, region, supersedes_id, effective_from,
                            effective_to, version, source_url, parameters, created_at
                        ) VALUES (
                            :id, :org_id, '并发门禁', NULL, '2026-01-01', '2026-12-31',
                            'concurrent-v1', 'https://www.chinatax.gov.cn/',
                            '{}'::jsonb, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {"id": policy_id, "org_id": org_id},
                )
            with Session(engine) as session:
                generated = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=org_id,
                        period_month="2026-07",
                        idempotency_key="extra-generate-july",
                        confirmation_note="并发期间",
                        evidence_references=[evidence_id],
                    )
                )
                session.commit()
                period_id = generated.period_id
            preview_request = PreviewAccountingPeriodCloseRequest(
                org_id=org_id, period_id=period_id, closing_date=date(2026, 7, 31)
            )
            with Session(engine) as session:
                preview = AccountingPeriodService(
                    session, current_date=date(2026, 8, 11)
                ).preview_accounting_period_close(preview_request)
            barrier = Barrier(2)

            def close() -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    result = AccountingPeriodService(
                        session, current_date=date(2026, 8, 11)
                    ).confirm_accounting_period_close(
                        ConfirmAccountingPeriodCloseRequest(
                            **preview_request.model_dump(),
                            calculation_hash=preview.calculation_hash,
                            idempotency_key="extra-close-july",
                            review_facts=AccountingPeriodReviewFacts(
                                voucher_completeness_reviewed=True,
                                bank_reconciliation_reviewed=True,
                                open_items_reviewed=True,
                                payroll_and_statutory_items_reviewed=True,
                                tax_items_reviewed=True,
                                asset_and_borrowing_schedules_reviewed=True,
                            ),
                            confirmation_note="并发月结",
                            evidence_references=[evidence_id],
                        )
                    )
                    session.commit()
                    return str(result.status), result.errors

            def payroll() -> tuple[str, str]:
                barrier.wait()
                try:
                    with engine.begin() as connection:
                        _insert_raw_payroll_batch(
                            connection,
                            org_id=org_id,
                            policy_id=policy_id,
                            posting_date=date(2026, 7, 20),
                            key="extra-racing-payroll",
                        )
                    return "posted", ""
                except DBAPIError as exc:
                    return "rejected", str(exc)

            with ThreadPoolExecutor(max_workers=2) as executor:
                close_future = executor.submit(close)
                payroll_future = executor.submit(payroll)
                close_result, payroll_result = close_future.result(), payroll_future.result()
            assert [close_result[0], payroll_result[0]].count("posted") == 1
            if close_result[0] == "posted":
                assert "ACCOUNTING_PERIOD_CLOSED" in payroll_result[1]
            else:
                assert payroll_result[0] == "posted"
                assert close_result[1]
                assert all(code.startswith("ACCOUNTING_PERIOD_") for code in close_result[1])

            parent_id, child_ids = uuid.uuid4(), [uuid.uuid4(), uuid.uuid4()]
            with engine.begin() as connection:
                connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
                _insert_raw_event(
                    connection,
                    org_id=org_id,
                    posting_date=date(2026, 7, 10),
                    status="posted",
                    key="dependency-parent",
                    event_type="customer_advance",
                    facts={"amounts": {"gross_amount_fen": 100}},
                    event_id=parent_id,
                )
                for index, child_id in enumerate(child_ids):
                    _insert_raw_event(
                        connection,
                        org_id=org_id,
                        posting_date=date(2026, 7, 11),
                        status="posted",
                        key=f"dependency-child-{index}",
                        event_type="service_fulfillment",
                        facts={
                            "amounts": {"amount_fen": 60},
                            "details": {"original_event_id": str(parent_id)},
                        },
                        event_id=child_id,
                    )
            dependency_barrier = Barrier(2)

            def dependency(child_id: uuid.UUID) -> tuple[str, str]:
                dependency_barrier.wait()
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO business_event_dependencies (
                                    id, org_id, parent_event_id, child_event_id,
                                    dependency_kind, amount_fen, created_at
                                ) VALUES (
                                    :id, :org_id, :parent_id, :child_id,
                                    'advance_fulfillment', 60, CURRENT_TIMESTAMP
                                )
                                """
                            ),
                            {
                                "id": uuid.uuid4(),
                                "org_id": org_id,
                                "parent_id": parent_id,
                                "child_id": child_id,
                            },
                        )
                    return "posted", ""
                except DBAPIError as exc:
                    return "rejected", str(exc)

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(dependency, child_ids))
            assert [status for status, _ in results].count("posted") == 1
            assert any("BUSINESS_EVENT_DEPENDENCY_INVALID" in error for _, error in results)

            draft_parent_id, reversed_child_id = uuid.uuid4(), uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
                _insert_raw_event(
                    connection,
                    org_id=org_id,
                    posting_date=date(2026, 7, 12),
                    status="draft",
                    key="dependency-draft-parent",
                    event_type="customer_advance",
                    facts={"amounts": {"gross_amount_fen": 100}},
                    event_id=draft_parent_id,
                )
                _insert_raw_event(
                    connection,
                    org_id=org_id,
                    posting_date=date(2026, 7, 13),
                    status="reversed",
                    key="dependency-reversed-child",
                    event_type="service_fulfillment",
                    facts={
                        "amounts": {"amount_fen": 50},
                        "details": {"original_event_id": str(draft_parent_id)},
                    },
                    event_id=reversed_child_id,
                )
            with pytest.raises(DBAPIError, match="BUSINESS_EVENT_DEPENDENCY_INVALID"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO business_event_dependencies (
                                id, org_id, parent_event_id, child_event_id,
                                dependency_kind, amount_fen, created_at
                            ) VALUES (
                                :id, :org_id, :parent_id, :child_id,
                                'advance_fulfillment', 50, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": org_id,
                            "parent_id": draft_parent_id,
                            "child_id": reversed_child_id,
                        },
                    )

            with Session(engine) as session:
                second_org = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG生成org锁",
                )
                session.flush()
                second_evidence = Evidence(
                    org_id=second_org.id,
                    sha256="f" * 64,
                    original_name="generation-org-lock.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/generation-org-lock.txt",
                )
                session.add(second_evidence)
                session.commit()
                second_org_id, second_evidence_id = second_org.id, second_evidence.id
            generation_barrier = Barrier(2)

            def generate(month: str) -> tuple[str, list[str]]:
                with Session(engine) as session:
                    generation_barrier.wait()
                    result = AccountingPeriodService(
                        session, current_date=date(2026, 8, 11)
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=second_org_id,
                            period_month=month,
                            idempotency_key=f"org-lock-{month}",
                            confirmation_note="跨月并发",
                            evidence_references=[second_evidence_id],
                        )
                    )
                    session.commit()
                    return str(result.status), result.errors

            with ThreadPoolExecutor(max_workers=2) as executor:
                generation_results = list(executor.map(generate, ("2026-05", "2026-07")))
            assert [status for status, _ in generation_results].count("posted") == 1
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT count(*) FROM accounting_periods WHERE org_id = :org_id"),
                        {"org_id": second_org_id},
                    )
                    == 1
                )

            with Session(engine) as session:
                direct_org = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG服务直写锁序",
                )
                session.flush()
                direct_evidence = Evidence(
                    org_id=direct_org.id,
                    sha256="0" * 64,
                    original_name="service-direct-lock.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/service-direct-lock.txt",
                )
                session.add(direct_evidence)
                session.commit()
                direct_org_id, direct_evidence_id = direct_org.id, direct_evidence.id
            direct_action_id, direct_calendar_id = uuid.uuid4(), uuid.uuid4()
            direct_input = {
                "org_id": str(direct_org_id),
                "period_month": "2026-07",
                "idempotency_key": "direct-sql-july",
                "confirmation_note": "服务与直写锁序",
                "evidence_references": [str(direct_evidence_id)],
            }
            with engine.begin() as connection:
                connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
                seed_parameters = {
                    "id": direct_calendar_id,
                    "org_id": direct_org_id,
                    "version": ACCOUNTING_PERIOD_CLOSE_RULE_VERSION,
                    "effective": ACCOUNTING_PERIOD_CLOSE_EFFECTIVE_FROM,
                    "urls": json.dumps(list(ACCOUNTING_PERIOD_CLOSE_SOURCE_URLS)),
                    "action_id": direct_action_id,
                    "key": direct_input["idempotency_key"],
                    "hash": canonical_sha256(
                        {
                            "command": "finance_generate_accounting_period",
                            "request": direct_input,
                        }
                    ),
                    "input": json.dumps(direct_input),
                    "evidence_id": direct_evidence_id,
                }
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO accounting_period_calendars (
                            id, org_id, calendar_year, rule_version,
                            rule_effective_from, source_urls, created_at
                        ) VALUES (
                            :id, :org_id, 2026, :version, :effective,
                            CAST(:urls AS jsonb), CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    seed_parameters,
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO accounting_period_actions (
                            id, org_id, action_type, idempotency_key,
                            request_payload_hash, status, input_facts,
                            missing_information, errors, confirmed_by,
                            confirmation_note, created_at
                        ) VALUES (
                            :action_id, :org_id, 'period_generation', :key,
                            :hash, 'posted', CAST(:input AS jsonb), '[]'::jsonb,
                            '[]'::jsonb, 'pg-reviewer', '服务与直写锁序',
                            CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    seed_parameters,
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO accounting_period_action_evidence (
                            action_id, org_id, evidence_id, created_at
                        ) VALUES (:action_id, :org_id, :evidence_id, CURRENT_TIMESTAMP)
                        """
                    ),
                    seed_parameters,
                )
            service_direct_barrier = Barrier(2)

            def service_generate() -> tuple[str, list[str]]:
                with Session(engine) as session:
                    service_direct_barrier.wait()
                    result = AccountingPeriodService(
                        session, current_date=date(2026, 8, 11)
                    ).generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=direct_org_id,
                            period_month="2026-05",
                            idempotency_key="service-may",
                            confirmation_note="服务与直写锁序",
                            evidence_references=[direct_evidence_id],
                        )
                    )
                    session.commit()
                    return str(result.status), result.errors

            def direct_generate() -> tuple[str, str]:
                service_direct_barrier.wait()
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO accounting_periods (
                                    id, org_id, calendar_id, generation_action_id,
                                    calendar_year, calendar_month, start_date,
                                    end_date, status, closed_at, close_id
                                ) VALUES (
                                    :id, :org_id, :calendar_id, :action_id,
                                    2026, 7, '2026-07-01', '2026-07-31',
                                    'open', NULL, NULL
                                )
                                """
                            ),
                            {
                                "id": uuid.uuid4(),
                                "org_id": direct_org_id,
                                "calendar_id": direct_calendar_id,
                                "action_id": direct_action_id,
                            },
                        )
                        connection.execute(
                            sa.text(
                                "UPDATE organizations SET "
                                "accounting_period_control_start_date = '2026-07-01' "
                                "WHERE id = :org_id"
                            ),
                            {"org_id": direct_org_id},
                        )
                    return "posted", ""
                except DBAPIError as exc:
                    return "rejected", str(exc)

            with ThreadPoolExecutor(max_workers=2) as executor:
                service_future = executor.submit(service_generate)
                direct_future = executor.submit(direct_generate)
                service_result, direct_result = service_future.result(), direct_future.result()
            assert [service_result[0], direct_result[0]].count("posted") == 1
            assert not any("40P01" in text for text in (str(service_result), str(direct_result)))
            with engine.connect() as connection:
                starts = connection.scalars(
                    sa.text(
                        "SELECT start_date FROM accounting_periods "
                        "WHERE org_id = :org_id ORDER BY start_date"
                    ),
                    {"org_id": direct_org_id},
                ).all()
                assert starts in ([date(2026, 5, 1)], [date(2026, 7, 1)])
        finally:
            engine.dispose()
