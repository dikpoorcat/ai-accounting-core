from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
import sqlalchemy as sa
from _postgres_helpers import (
    authenticated_business_database,
    confirmed_payroll,
    isolated_postgres_url,
)
from alembic.config import Config
from conftest import prepare_authenticated_bank_account
from sqlalchemy import inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    ConfirmAccountingPeriodCloseRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.business_metadata import (
    UpdateBusinessMetadataRequest,
    update_business_metadata,
)
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.financial_statement_schemas import (
    ConfirmFinancialStatementOpeningBalanceRequest,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    BusinessEvent,
    BusinessMetadataVersion,
    OpenItem,
    Organization,
    PayrollContributionSupplement,
    PayrollEventLink,
    VoucherLine,
)
from alembic import command


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url_override"] = database_url
    return config


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_essential_baseline_matches_models_and_installs_guards() -> None:
    with isolated_postgres_url("essential_migration") as database_url:
        config = _config(database_url)
        command.upgrade(config, "head")
        engine = sa.create_engine(database_url)
        try:
            command.check(config)
            inspector = inspect(engine)
            assert "business_metadata_versions" in inspector.get_table_names()
            assert (
                next(
                    column
                    for column in inspector.get_columns("open_items")
                    if column["name"] == "counterparty_id"
                )["nullable"]
                is True
            )
            with engine.connect() as connection:
                assert (
                    connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                    == "0005_payroll_provenance"
                )
                purchase = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_purchase_component(uuid)'::regprocedure)"
                    )
                )
                borrowing = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef('finance_assert_borrowing(uuid)'::regprocedure)"
                    )
                )
                close = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
                    )
                )
                action = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_accounting_period_action(uuid)'::regprocedure)"
                    )
                )
                metadata_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_guard_business_metadata_insert_0003()'::regprocedure)"
                    )
                )
                amendment_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef('finance_guard_event_amendment()'::regprocedure)"
                    )
                )
                activation_projection = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_fixed_asset_activation_projection(uuid)'::regprocedure)"
                    )
                )
                payroll_link_index = connection.scalar(
                    sa.text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname='public' "
                        "AND indexname='uq_payroll_event_link_payment_source'"
                    )
                )
                payroll_link_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_final_payroll_event_links(uuid)'::regprocedure)"
                    )
                )
                assert "PURCHASE_PROJECT_REFERENCE_REQUIRED" not in purchase
                assert "contract_reference" not in purchase
                assert "PROJECT_COST_SOURCE_AMOUNT_EXCEEDED" in purchase
                assert "interest_due_dates::jsonb ->>" not in borrowing
                assert "accrual.period_start<>expected_start" in borrowing
                assert "owner_workflow_close_gates" not in close
                assert "lag(accrual.period_end)" in close
                assert "ACCOUNTING_PERIOD_REVIEW_INCOMPLETE" not in action
                assert "action_type = 'period_generation' AND NOT EXISTS" not in action
                assert "tool_name IN" not in metadata_guard
                assert "BUSINESS_METADATA_COUNTERPARTY_NOT_IN_COMPANY" in metadata_guard
                assert "business_metadata_versions" in amendment_guard
                assert "'asset_code', asset.asset_code" not in activation_projection
                assert "'asset_name', asset.name" not in activation_projection
                assert (
                    "org_id, component_id, link_kind, payroll_batch_id, "
                    "source_payment_event_id, source_open_item_id"
                ) in payroll_link_index
                assert "covered.payroll_batch_id=origin.payroll_batch_id" in payroll_link_guard
                trigger_names = set(
                    connection.scalars(
                        sa.text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgrelid='business_metadata_versions'::regclass "
                            "AND NOT tgisinternal"
                        )
                    )
                )
                assert trigger_names == {
                    "business_metadata_append_only_0003",
                    "business_metadata_execution_attribution_guard",
                    "business_metadata_insert_guard_0003",
                }
        finally:
            engine.dispose()


def _record_pass_through(
    session: Session,
    org_id,
    evidence_id,
    *,
    key: str,
    amount: int,
    posting_date: date = date(2026, 3, 5),
):
    return ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "posting_date": posting_date,
                "evidence_references": [evidence_id],
                "components": [
                    {
                        "key": "collection",
                        "kind": "pass_through",
                        "amount_fen": amount,
                        "metadata": {"purpose": "仅管理说明"},
                    }
                ],
                "funds": [
                    {
                        "key": "cash",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": posting_date,
                        "amount_fen": amount,
                        "allocations": [{"component_key": "collection", "amount_fen": amount}],
                    }
                ],
            }
        )
    )


def _approve_close(session: Session, attribution, period_id, calculation_hash: str):
    now = datetime.now(UTC)
    approval = AccountingPeriodCloseApproval(
        org_id=attribution.org_id,
        period_id=period_id,
        owner_account_id=attribution.owner_account_id,
        owner_session_id=attribution.owner_session_id,
        owner_credential_version=attribution.owner_credential_version,
        catalog_instance_id=attribution.catalog_instance_id,
        calculation_hash=calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(approval)
    session.flush()
    return approval.id


def _confirm_zero_opening(session: Session, authority, org_id, evidence_id) -> None:
    with authority.attributed_call(
        session, tool_name="finance_confirm_financial_statement_opening_balance"
    ):
        result = FinancialStatementService(session).confirm_opening_balance(
            ConfirmFinancialStatementOpeningBalanceRequest(
                org_id=org_id,
                establishment_date=date(2026, 7, 1),
                treatment="zero_on_establishment",
                idempotency_key=f"essential-opening-{org_id}",
                evidence_references=[evidence_id],
            )
        )
    assert result.status == "posted", result.errors


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_anonymous_open_item_and_metadata_history_keep_accounting_immutable() -> None:
    with authenticated_business_database("essential_metadata") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session, org_id, evidence_id, key="anonymous-collection", amount=10_000
                )
            assert str(posted.status) == "posted", posted.errors
            session.commit()
            event = session.get(BusinessEvent, posted.event_id)
            assert event is not None
            item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == event.id))
            assert item is not None
            assert item.counterparty_id is None
            before_event = (event.facts, event.request_payload_hash, event.rule_trace)
            before_lines = list(
                session.execute(
                    select(
                        VoucherLine.id,
                        VoucherLine.account_id,
                        VoucherLine.counterparty_id,
                        VoucherLine.debit_fen,
                        VoucherLine.credit_fen,
                    )
                    .where(VoucherLine.org_id == org_id)
                    .order_by(VoucherLine.id)
                )
            )

            request = UpdateBusinessMetadataRequest.model_validate(
                {
                    "org_id": org_id,
                    "source": {
                        "event_key": "anonymous-collection",
                        "component_key": "collection",
                    },
                    "metadata": {"purpose": "关账后仍可补录的管理说明"},
                    "expected_version": 1,
                    "idempotency_key": "anonymous-collection-metadata-v2",
                }
            )
            with authority.attributed_call(session, tool_name="finance_update_business_metadata"):
                updated = update_business_metadata(session, request)
            assert updated["status"] == "updated", updated
            session.commit()
            with authority.attributed_call(session, tool_name="finance_update_business_metadata"):
                replay = update_business_metadata(session, request)
            assert replay["idempotent_replay"] is True
            assert replay["version"] == 2
            session.commit()
            session.refresh(event)
            assert (event.facts, event.request_payload_hash, event.rule_trace) == before_event
            assert (
                list(
                    session.execute(
                        select(
                            VoucherLine.id,
                            VoucherLine.account_id,
                            VoucherLine.counterparty_id,
                            VoucherLine.debit_fen,
                            VoucherLine.credit_fen,
                        )
                        .where(VoucherLine.org_id == org_id)
                        .order_by(VoucherLine.id)
                    )
                )
                == before_lines
            )
            versions = session.scalars(
                select(BusinessMetadataVersion).order_by(BusinessMetadataVersion.version)
            ).all()
            assert [row.version for row in versions] == [1, 2]
            assert all(row.execution_attribution_id is not None for row in versions)

            with pytest.raises(DBAPIError, match="BUSINESS_METADATA_HISTORY_APPEND_ONLY"):
                with authority.attributed_call(
                    session, tool_name="finance_update_business_metadata"
                ):
                    session.execute(
                        sa.text(
                            "UPDATE business_metadata_versions "
                            'SET metadata_values=\'{"purpose":"tampered"}\'::jsonb '
                            "WHERE id=:id"
                        ),
                        {"id": versions[-1].id},
                    )
                    session.commit()
            session.rollback()


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_metadata_expected_version_serializes_concurrent_updates() -> None:
    with authenticated_business_database("essential_metadata_concurrency") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session,
                    org_id,
                    evidence_id,
                    key="concurrent-metadata-source",
                    amount=1_000,
                )
            assert str(posted.status) == "posted", posted.errors
            session.commit()

        ready = Barrier(2)

        def update(suffix: str):
            with Session(engine) as session:
                request = UpdateBusinessMetadataRequest.model_validate(
                    {
                        "org_id": org_id,
                        "source": {
                            "event_key": "concurrent-metadata-source",
                            "component_key": "collection",
                        },
                        "metadata": {"description": f"writer-{suffix}"},
                        "expected_version": 1,
                        "idempotency_key": f"concurrent-metadata-{suffix}",
                    }
                )
                ready.wait(timeout=10)
                with authority.attributed_call(
                    session, tool_name="finance_update_business_metadata"
                ):
                    result = update_business_metadata(session, request)
                session.commit()
                return result

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, ("a", "b")))

        assert [result["status"] for result in results].count("updated") == 1
        assert [result.get("errors") for result in results].count(
            ["METADATA_VERSION_CONFLICT"]
        ) == 1
        with Session(engine) as session:
            versions = session.scalars(select(BusinessMetadataVersion.version)).all()
            assert sorted(versions) == [1, 2]


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_metadata_history_does_not_block_open_period_event_deletion() -> None:
    with authenticated_business_database("essential_metadata_lifecycle") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session,
                    org_id,
                    evidence_id,
                    key="metadata-delete-source",
                    amount=1_000,
                )
            assert str(posted.status) == "posted", posted.errors
            session.commit()
            event = session.get(BusinessEvent, posted.event_id)
            assert event is not None
            with authority.attributed_call(session, tool_name="finance_delete_event"):
                deleted = EventAmendmentService(session).amend(
                    DeleteEventRequest(
                        org_id=org_id,
                        event_id=event.id,
                        idempotency_key="delete-event-with-metadata",
                        expected_facts_hash=canonical_sha256(event.facts),
                        reason="remove isolated test event",
                    )
                )
            assert deleted["status"] == "deleted", deleted
            session.commit()
            assert session.get(BusinessEvent, event.id).status == "deleted"
            history = session.scalars(
                select(BusinessMetadataVersion).where(BusinessMetadataVersion.event_id == event.id)
            ).all()
            assert len(history) == 1


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_closed_period_metadata_update_keeps_close_and_ledger_snapshot_immutable() -> None:
    with authenticated_business_database("essential_metadata_closed") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            assert organization is not None
            prepare_authenticated_bank_account(
                session,
                organization,
                booking_date=date(2026, 7, 1),
                authority=authority,
                evidence_id=evidence_id,
                accounts=[],
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session,
                    org_id,
                    evidence_id,
                    key="closed-period-metadata-source",
                    amount=10_000,
                    posting_date=date(2026, 7, 5),
                )
            assert str(posted.status) == "posted", posted.errors
            _confirm_zero_opening(session, authority, org_id, evidence_id)
            session.commit()

            period = session.scalar(
                select(AccountingPeriod).where(
                    AccountingPeriod.org_id == org_id,
                    AccountingPeriod.start_date == date(2026, 7, 1),
                )
            )
            assert period is not None
            period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
            preview_request = PreviewAccountingPeriodCloseRequest(
                org_id=org_id,
                period_id=period.id,
                closing_date=date(2026, 7, 31),
            )
            preview = period_service.preview_accounting_period_close(preview_request)
            assert preview.status == "calculated", preview.errors
            with authority.attributed_call(
                session, tool_name="finance_confirm_accounting_period_close"
            ) as attribution:
                approval_id = _approve_close(
                    session, attribution, period.id, preview.calculation_hash
                )
                confirmed = period_service.confirm_accounting_period_close(
                    ConfirmAccountingPeriodCloseRequest(
                        **preview_request.model_dump(),
                        calculation_hash=preview.calculation_hash,
                        owner_approval_id=approval_id,
                        idempotency_key="close-without-management-review",
                    )
                )
            assert confirmed.status == "posted", {
                "errors": confirmed.errors,
                "blockers": preview.data["blocker_codes"],
            }
            session.commit()

            event = session.get(BusinessEvent, posted.event_id)
            close = session.get(AccountingPeriodClose, confirmed.close_id)
            assert event is not None and close is not None
            before_close = (
                close.calculation_hash,
                close.calculation_payload,
                close.calculation,
            )
            before_event = (
                event.request_payload_hash,
                event.facts,
                event.rule_trace,
            )
            before_lines = list(
                session.execute(
                    select(
                        VoucherLine.id,
                        VoucherLine.account_id,
                        VoucherLine.debit_fen,
                        VoucherLine.credit_fen,
                    )
                    .where(VoucherLine.org_id == org_id)
                    .order_by(VoucherLine.id)
                )
            )
            before_balances = list(
                session.execute(
                    select(
                        VoucherLine.account_id,
                        sa.func.sum(VoucherLine.debit_fen - VoucherLine.credit_fen),
                    )
                    .where(VoucherLine.org_id == org_id)
                    .group_by(VoucherLine.account_id)
                    .order_by(VoucherLine.account_id)
                )
            )
            before_item = session.execute(
                select(
                    OpenItem.id,
                    OpenItem.original_amount_fen,
                    OpenItem.settled_amount_fen,
                    OpenItem.status,
                ).where(OpenItem.source_event_id == event.id)
            ).one()

            request = UpdateBusinessMetadataRequest.model_validate(
                {
                    "org_id": org_id,
                    "source": {
                        "event_key": "closed-period-metadata-source",
                        "component_key": "collection",
                    },
                    "metadata": {
                        "beneficiary": {"kind": "other", "name": "later beneficiary"},
                        "description": "added after close",
                    },
                    "expected_version": 1,
                    "idempotency_key": "closed-period-metadata-v2",
                }
            )
            with authority.attributed_call(session, tool_name="finance_update_business_metadata"):
                updated = update_business_metadata(session, request)
            assert updated["status"] == "updated", updated
            session.commit()

            session.refresh(close)
            session.refresh(event)
            assert (
                close.calculation_hash,
                close.calculation_payload,
                close.calculation,
            ) == before_close
            assert (
                event.request_payload_hash,
                event.facts,
                event.rule_trace,
            ) == before_event
            assert (
                list(
                    session.execute(
                        select(
                            VoucherLine.id,
                            VoucherLine.account_id,
                            VoucherLine.debit_fen,
                            VoucherLine.credit_fen,
                        )
                        .where(VoucherLine.org_id == org_id)
                        .order_by(VoucherLine.id)
                    )
                )
                == before_lines
            )
            assert (
                list(
                    session.execute(
                        select(
                            VoucherLine.account_id,
                            sa.func.sum(VoucherLine.debit_fen - VoucherLine.credit_fen),
                        )
                        .where(VoucherLine.org_id == org_id)
                        .group_by(VoucherLine.account_id)
                        .order_by(VoucherLine.account_id)
                    )
                )
                == before_balances
            )
            assert (
                session.execute(
                    select(
                        OpenItem.id,
                        OpenItem.original_amount_fen,
                        OpenItem.settled_amount_fen,
                        OpenItem.status,
                    ).where(OpenItem.source_event_id == event.id)
                ).one()
                == before_item
            )

            with pytest.raises(DBAPIError, match="BUSINESS_METADATA_VERSION_OUT_OF_SEQUENCE"):
                with authority.attributed_call(
                    session, tool_name="finance_update_business_metadata"
                ) as attribution:
                    session.execute(
                        sa.text("""
                        INSERT INTO business_metadata_versions (
                            id,org_id,event_id,component_key,version,metadata_values,
                            idempotency_key,request_hash,execution_attribution_id,created_at
                        ) VALUES (
                            :id,:org,:event,'collection',4,'{}'::jsonb,
                            'skip-version',:hash,:attribution,CURRENT_TIMESTAMP
                        )
                    """),
                        {
                            "id": uuid.uuid4(),
                            "org": org_id,
                            "event": event.id,
                            "hash": "a" * 64,
                            "attribution": attribution.id,
                        },
                    )
                    session.commit()
            session.rollback()


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_metadata_direct_insert_requires_current_authority_and_valid_component() -> None:
    with authenticated_business_database("essential_metadata_guard") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = _record_pass_through(
                    session, org_id, evidence_id, key="guard-source", amount=1_000
                )
            session.commit()
            event_id = posted.event_id
            attribution_id = session.scalar(
                select(BusinessMetadataVersion.execution_attribution_id)
            )

        with pytest.raises(DBAPIError, match="BUSINESS_.*EXECUTION_ATTRIBUTION"):
            with engine.begin() as connection:
                connection.execute(
                    sa.text("""
                    INSERT INTO business_metadata_versions (
                        id,org_id,event_id,component_key,version,metadata_values,
                        idempotency_key,request_hash,execution_attribution_id,created_at
                    ) VALUES (
                        :id,:org,:event,'collection',2,'{}'::jsonb,
                        'raw-bypass',:hash,:attribution,CURRENT_TIMESTAMP
                    )
                """),
                    {
                        "id": uuid.uuid4(),
                        "org": org_id,
                        "event": event_id,
                        "hash": "b" * 64,
                        "attribution": attribution_id,
                    },
                )

        with Session(engine) as session:
            with pytest.raises(DBAPIError, match="BUSINESS_METADATA_SOURCE_OR_PAYLOAD_INVALID"):
                with authority.attributed_call(
                    session, tool_name="finance_update_business_metadata"
                ) as attribution:
                    session.execute(
                        sa.text("""
                        INSERT INTO business_metadata_versions (
                            id,org_id,event_id,component_key,version,metadata_values,
                            idempotency_key,request_hash,execution_attribution_id,created_at
                        ) VALUES (
                            :id,:org,:event,'missing-component',1,'[]'::jsonb,
                            'invalid-source',:hash,:attribution,CURRENT_TIMESTAMP
                        )
                    """),
                        {
                            "id": uuid.uuid4(),
                            "org": org_id,
                            "event": event_id,
                            "hash": "c" * 64,
                            "attribution": attribution.id,
                        },
                    )
                    session.commit()
            session.rollback()

            invalid_metadata = [
                {"unknown_key": "value"},
                {"purpose": 123},
                {"due_date": "2026-02-30"},
                {"interest_due_dates": "2026-03-31"},
                {"beneficiary": {"kind": "other", "name": "name", "extra": "bad"}},
            ]
            for index, metadata_values in enumerate(invalid_metadata):
                with pytest.raises(
                    DBAPIError,
                    match=(
                        "BUSINESS_METADATA_(SOURCE_OR_PAYLOAD_INVALID|COUNTERPARTY_NOT_IN_COMPANY)"
                    ),
                ):
                    with authority.attributed_call(
                        session, tool_name="finance_update_business_metadata"
                    ) as attribution:
                        session.execute(
                            sa.text(
                                """
                                INSERT INTO business_metadata_versions (
                                    id,org_id,event_id,component_key,version,metadata_values,
                                    idempotency_key,request_hash,
                                    execution_attribution_id,created_at
                                ) VALUES (
                                    :id,:org,:event,'collection',2,
                                    CAST(:metadata_values AS jsonb),
                                    :idempotency_key,:hash,:attribution,CURRENT_TIMESTAMP
                                )
                                """
                            ),
                            {
                                "id": uuid.uuid4(),
                                "org": org_id,
                                "event": event_id,
                                "metadata_values": json.dumps(metadata_values),
                                "idempotency_key": f"invalid-metadata-shape-{index}",
                                "hash": "e" * 64,
                                "attribution": attribution.id,
                            },
                        )
                        session.commit()
                session.rollback()

            with pytest.raises(DBAPIError, match="BUSINESS_METADATA_COUNTERPARTY_NOT_IN_COMPANY"):
                with authority.attributed_call(
                    session, tool_name="finance_update_business_metadata"
                ) as attribution:
                    session.execute(
                        sa.text(
                            """
                            INSERT INTO business_metadata_versions (
                                id,org_id,event_id,component_key,version,metadata_values,
                                idempotency_key,request_hash,execution_attribution_id,created_at
                            ) VALUES (
                                :id,:org,:event,'collection',2,
                                jsonb_build_object(
                                    'beneficiary',jsonb_build_object(
                                        'id',CAST(:foreign_party AS text),'kind','other')),
                                'foreign-party-bypass',:hash,:attribution,CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org": org_id,
                            "event": event_id,
                            "foreign_party": str(uuid.uuid4()),
                            "hash": "d" * 64,
                            "attribution": attribution.id,
                        },
                    )
                    session.commit()
            session.rollback()


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_contribution_supplement_without_management_or_batch_source_passes_pg_guards() -> None:
    with authenticated_business_database("essential_supplement_minimal") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            _, _, payroll_line, _, _ = confirmed_payroll(
                session,
                org_id,
                evidence_id,
                authority,
                key="essential-supplement-policy-source",
            )
            session.commit()
            request = RecordEventRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "essential-minimal-supplement-pg",
                    "posting_date": date(2026, 4, 30),
                    "evidence_references": [evidence_id],
                    "components": [
                        {
                            "key": "supplement",
                            "kind": "payroll_contribution_supplement",
                            "business_date": date(2026, 4, 30),
                            "employee_id": payroll_line.employee_id,
                            "contribution_period": "2026-03",
                            "items": [
                                {
                                    "contribution_group": "social_insurance",
                                    "insurance_kind": "pension",
                                    "employee_amount_fen": 10,
                                    "employer_amount_fen": 30,
                                    "employee_amount_treatment": "employee_receivable",
                                }
                            ],
                        }
                    ],
                }
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                result = ComponentService(session).record(request)
            assert str(result.status) == "posted", result.errors
            session.commit()
            supplement = session.scalar(
                select(PayrollContributionSupplement).where(
                    PayrollContributionSupplement.event_id == result.event_id
                )
            )
            assert supplement is not None
            assert supplement.source_payroll_batch_id is None
            assert supplement.assessment_reference is None
            assert supplement.reason_code is None
            assert supplement.reason_description is None
            assert (
                session.scalar(
                    select(sa.func.count())
                    .select_from(PayrollEventLink)
                    .where(PayrollEventLink.event_id == result.event_id)
                )
                == 0
            )
