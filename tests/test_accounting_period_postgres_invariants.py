from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
import sqlalchemy as sa
from _postgres_helpers import catalog_owner_authority, isolated_postgres_url
from alembic.config import Config
from conftest import prepare_authenticated_bank_account
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.financial_statement_schemas import (
    ConfirmFinancialStatementClassificationRequest,
    ConfirmFinancialStatementOpeningBalanceRequest,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    Evidence,
    ExecutionAttribution,
    Organization,
    VoucherLine,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url_override"] = database_url
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
        catalog_instance_id=attribution.catalog_instance_id,
        calculation_hash=calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    session.add(approval)
    session.flush()
    return approval.id


def _confirm_partial_year_zero_opening(
    session: Session,
    authority: Any,
    *,
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
) -> None:
    with authority.attributed_call(
        session,
        tool_name="finance_confirm_financial_statement_opening_balance",
    ):
        result = FinancialStatementService(session).confirm_opening_balance(
            ConfirmFinancialStatementOpeningBalanceRequest(
                org_id=org_id,
                establishment_date=date(2026, 7, 1),
                treatment="zero_on_establishment",
                idempotency_key=f"postgres-partial-opening-{org_id}",
                confirmation_note="测试企业于七月新设，成立时点期初余额为零。",
                evidence_references=[evidence_id],
            )
        )
    assert result.status == "posted", result.errors


def _sale(
    org_id: object,
    key: str,
    *,
    evidence_id: uuid.UUID,
    business_date: date = date(2026, 7, 15),
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": business_date,
            "evidence_references": [evidence_id],
            "components": [
                {
                    "key": "sale",
                    "kind": "service_sale",
                    "business_date": business_date,
                    "fulfillment_date": business_date,
                    "tax_obligation_date": business_date,
                    "amount_fen": 101000,
                    "recognition_basis": "credit",
                    "tax_facts": {
                        "taxable": True,
                        "rate_percent": "1",
                        "invoice_type": "ordinary",
                        "waive_exemption": False,
                        "tax_due_on_event": True,
                    },
                    "metadata": {"counterparty": {"kind": "customer", "name": "期间测试客户"}},
                }
            ],
        }
    )


def _mixed_expense(
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
    *,
    key: str,
    travel_fen: int = 20_000,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-07-20",
            "evidence_references": [evidence_id],
            "components": [
                {
                    "key": "travel",
                    "kind": "expense",
                    "business_date": "2026-07-20",
                    "amount_fen": travel_fen,
                    "expense_class": "general_expense",
                    "payment_basis": "supplier_credit",
                    "metadata": {"counterparty": {"kind": "supplier", "name": "组合业务供应商甲"}},
                },
                {
                    "key": "selling",
                    "kind": "expense",
                    "business_date": "2026-07-20",
                    "amount_fen": 10000,
                    "expense_class": "sales_expense",
                    "payment_basis": "supplier_credit",
                    "metadata": {"counterparty": {"kind": "supplier", "name": "组合业务供应商乙"}},
                },
            ],
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
    with isolated_postgres_url("finance_company") as database_url:
        config = _config(database_url)
        command.upgrade(config, "head")
        command.check(config)
        engine = sa.create_engine(database_url)
        authority_stack = ExitStack()
        try:
            with engine.connect() as connection:
                close_assertion = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef(to_regprocedure("
                        "'finance_assert_accounting_period_close(uuid)'))"
                    )
                )
                source_validator = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef(to_regprocedure("
                        "'finance_validate_accounting_period_close_source()'))"
                    )
                )
                source_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef(to_regprocedure("
                        "'finance_guard_accounting_period_close_source_insert()'))"
                    )
                )
                assert "PERFORM finance_assert_fixed_asset(asset.id)" not in close_assertion
                assert "PERFORM finance_assert_intangible_asset(asset.id)" not in close_assertion
                assert "ACCOUNTING_PERIOD_PERIOD_NOT_ENDED" in close_assertion
                assert "FROM employees AS employee" in close_assertion
                assert "FROM payroll_lines AS line" in close_assertion
                assert "owner_workflow_close_gates_2026.3" in close_assertion
                assert "individual_income_tax_declaration,satisfied" in close_assertion
                assert "FROM external_obligation_confirmations AS confirmation" in (close_assertion)
                assert "payroll_tax_import_source_v1" not in close_assertion
                assert (
                    "PERFORM finance_assert_accounting_period_close(NEW.close_id)"
                    not in source_validator
                )
                assert "finance_parent_xmin_is_current_0015(close_xmin)" in source_guard

            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG期间提交点",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(
                        session,
                        organization,
                        registry_database_name=engine.url.database,
                    )
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="7" * 64,
                    original_name="period.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/period.txt",
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    session.add(evidence)
                    session.flush()
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            policy_id = uuid.uuid4()
            with Session(engine) as session:
                with authority.attributed_call(
                    session, tool_name="finance_register_payroll_policy"
                ) as attribution:
                    session.execute(
                        sa.text(
                            """
                        INSERT INTO payroll_policy_versions (
                            id, org_id, region, supersedes_id, effective_from,
                            effective_to, version, source_url, parameters,
                            execution_attribution_id, created_at
                        ) VALUES (
                            :id, :org_id, '期间门禁测试', NULL, '2026-01-01',
                            '2026-12-31', 'period-guard-v1',
                            'https://www.chinatax.gov.cn/', '{}'::jsonb, :attribution_id,
                            CURRENT_TIMESTAMP
                        )
                        """
                        ),
                        {
                            "id": policy_id,
                            "org_id": org_id,
                            "attribution_id": attribution.id,
                        },
                    )
                session.commit()

            with Session(engine) as session:
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
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
                with Session(engine) as session:
                    with authority.attributed_call(
                        session, tool_name="finance_negative_tamper"
                    ) as attribution:
                        _insert_raw_event(
                            session,
                            org_id=org_id,
                            posting_date=date(2026, 6, 30),
                            status="draft",
                            key="direct-draft-before-start",
                            execution_attribution_id=attribution.id,
                        )
                        session.commit()

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_NOT_GENERATED"):
                with Session(engine) as session:
                    with authority.attributed_call(
                        session, tool_name="finance_negative_tamper"
                    ) as attribution:
                        _insert_raw_payroll_batch(
                            session,
                            org_id=org_id,
                            policy_id=policy_id,
                            posting_date=date(2026, 6, 30),
                            key="direct-payroll-before-start",
                            execution_attribution_id=attribution.id,
                        )
                        session.commit()

            with Session(engine) as session:
                with authority.attributed_call(
                    session, tool_name="finance_negative_fixture"
                ) as attribution:
                    superseded_id = _insert_raw_payroll_batch(
                        session,
                        org_id=org_id,
                        policy_id=policy_id,
                        posting_date=date(2026, 7, 20),
                        key="direct-payroll-open-superseded",
                        execution_attribution_id=attribution.id,
                    )
                    session.execute(
                        sa.text("UPDATE payroll_batches SET status = 'superseded' WHERE id = :id"),
                        {"id": superseded_id},
                    )
                session.commit()

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_NOT_GENERATED"):
                with Session(engine) as session:
                    with authority.attributed_call(
                        session, tool_name="finance_negative_tamper"
                    ) as attribution:
                        event_id, voucher_id = uuid.uuid4(), uuid.uuid4()
                        session.execute(
                            sa.text(
                                """
                            INSERT INTO business_events (
                                id, org_id, idempotency_key, request_payload_hash,
                                event_type, status, description, facts, business_date,
                                fulfillment_date, invoice_date, payment_date,
                                tax_obligation_date, posting_date, rule_trace,
                                rule_version, reversed_by_event_id,
                                execution_attribution_id, created_at
                            ) VALUES (
                                :event_id, :org_id, 'direct-before-start', :hash,
                                'service_cash_sale', 'draft', '', '{}'::jsonb,
                                '2026-06-30', NULL, NULL, NULL, NULL, '2026-06-30',
                                '[]'::jsonb, NULL, NULL, :attribution_id, CURRENT_TIMESTAMP
                            )
                            """
                            ),
                            {
                                "event_id": event_id,
                                "org_id": org_id,
                                "hash": "a" * 64,
                                "attribution_id": attribution.id,
                            },
                        )
                        session.execute(
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
                            {
                                "voucher_id": voucher_id,
                                "org_id": org_id,
                                "event_id": event_id,
                            },
                        )
                        session.commit()

            with Session(engine) as session:
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    sale = FinanceService(session).record_event(
                        _sale(org_id, "pg-july-sale", evidence_id=evidence_id)
                    )
                assert sale.status == "posted", sale.errors
                session.commit()

            with Session(engine) as session:
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    mixed = ComponentService(session).record(
                        _mixed_expense(
                            org_id,
                            evidence_id,
                            key="pg-july-mixed-expense",
                        )
                    )
                assert mixed.status == "posted", mixed.errors
                expense_lines = list(
                    session.scalars(
                        sa.select(VoucherLine)
                        .where(
                            VoucherLine.voucher_id == mixed.voucher_id,
                            VoucherLine.debit_fen > 0,
                        )
                        .order_by(VoucherLine.line_number)
                    )
                )
                assert {line.account.code for line in expense_lines} == {"5601", "5602"}
                for line in expense_lines:
                    detail_code = (
                        "sales_other" if line.account.code == "5601" else "management_other"
                    )
                    with authority.attributed_call(
                        session,
                        tool_name="finance_confirm_financial_statement_classification",
                    ):
                        classified = FinancialStatementService(session).confirm_classification(
                            ConfirmFinancialStatementClassificationRequest(
                                org_id=org_id,
                                voucher_line_id=line.id,
                                allocations=[
                                    {
                                        "detail_code": detail_code,
                                        "amount_fen": line.debit_fen,
                                    }
                                ],
                                idempotency_key=f"pg-mixed-classification-{line.id}",
                                confirmation_note="组合费用已逐行确认财务报表明细分类。",
                                evidence_references=[evidence_id],
                            )
                        )
                    assert classified.status == "posted", classified.errors
                session.commit()
                mixed_event_id = mixed.event_id
                mixed_facts_hash = mixed.data["facts_hash"]

            with Session(engine) as session:
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
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
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    future_sale = FinanceService(session).record_event(
                        _sale(
                            org_id,
                            "pg-august-sale-before-july-close",
                            evidence_id=evidence_id,
                            business_date=date(2026, 8, 1),
                        )
                    )
                assert future_sale.status == "posted", future_sale.errors
                session.commit()
                august_period_id = august.period_id
                august_event_id = future_sale.event_id
                august_voucher_id = future_sale.voucher_id

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
                _confirm_partial_year_zero_opening(
                    session,
                    authority,
                    org_id=org_id,
                    evidence_id=evidence_id,
                )
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                preview_request = PreviewAccountingPeriodCloseRequest(
                    org_id=org_id,
                    period_id=period_id,
                    closing_date=date(2026, 7, 31),
                )
                preview = period_service.preview_accounting_period_close(preview_request)
                assert preview.data["calculation"]["review_counts"]["open_items"] == 3
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
                            management_commentary_context_hash=preview.data[
                                "assistant_review_checklist"
                            ]["management_commentary"]["context_hash"],
                            management_commentary="七月经营情况已基于关账上下文完成分析。",
                            owner_approval_id=owner_approval_id,
                            idempotency_key="pg-close-july",
                            review_facts=AccountingPeriodReviewFacts(
                                voucher_completeness_reviewed=True,
                                bank_reconciliation_reviewed=True,
                                open_items_reviewed=True,
                                payroll_and_statutory_items_reviewed=True,
                                payroll_settlements_reviewed=True,
                                tax_items_reviewed=True,
                                asset_and_borrowing_schedules_reviewed=True,
                            ),
                            confirmation_note="PG月结",
                            evidence_references=[evidence_id],
                        )
                    )
                assert confirmed.status == "posted", {
                    "blockers": preview.data["blocker_codes"],
                    "incomplete": [
                        (item["code"], item["state"])
                        for item in preview.data["assistant_review_checklist"]["items"]
                        if not item["completed"]
                    ],
                }
                session.commit()
                close_id = confirmed.close_id
                original_hash = confirmed.calculation_hash
                original_payload = session.get(AccountingPeriodClose, close_id).calculation_payload

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_CLOSE_ALREADY_SEALED"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO accounting_period_close_sources (
                                close_id, voucher_id, org_id, event_id, voucher_number,
                                posting_date, description, event_type,
                                event_status_at_close, request_payload_hash_at_close,
                                debit_fen, credit_fen, line_snapshot, created_at
                            ) VALUES (
                                :close_id, :voucher_id, :org_id, :event_id,
                                'ILLEGAL-LATE-SOURCE', '2026-08-01', '',
                                'service_credit_sale', 'posted', NULL,
                                1, 1, '[]'::jsonb, CURRENT_TIMESTAMP
                            )
                            """
                        ),
                        {
                            "close_id": close_id,
                            "voucher_id": august_voucher_id,
                            "org_id": org_id,
                            "event_id": august_event_id,
                        },
                    )

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
                assert close.voucher_count == 2
                lifecycle = EventAmendmentService(session)
                with authority.attributed_call(session, tool_name="finance_amend_event"):
                    amended = lifecycle.amend(
                        AmendEventRequest(
                            org_id=org_id,
                            event_id=mixed_event_id,
                            idempotency_key="pg-closed-mixed-amend",
                            expected_facts_hash=mixed_facts_hash,
                            reason="关账后不得原地改写组合业务",
                            replacement=_mixed_expense(
                                org_id,
                                evidence_id,
                                key="pg-july-mixed-expense",
                                travel_fen=25_000,
                            ),
                        )
                    )
                assert amended["status"] == "rejected"
                assert amended["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
                with authority.attributed_call(session, tool_name="finance_delete_event"):
                    deleted = lifecycle.amend(
                        DeleteEventRequest(
                            org_id=org_id,
                            event_id=mixed_event_id,
                            idempotency_key="pg-closed-mixed-delete",
                            expected_facts_hash=mixed_facts_hash,
                            reason="关账后不得删除组合业务",
                        )
                    )
                assert deleted["status"] == "rejected"
                assert deleted["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    rejected = FinanceService(session).record_event(
                        _sale(org_id, "after-close", evidence_id=evidence_id)
                    )
                assert rejected.errors == ["ACCOUNTING_PERIOD_CLOSED"]

            with Session(engine) as session:
                august = session.get(AccountingPeriod, august_period_id)
                assert august is not None and august.status == "open"
            with Session(engine) as session:
                with authority.attributed_call(session, tool_name="finance_reverse_event"):
                    reversal = FinanceService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=mixed_event_id,
                            idempotency_key="pg-reverse-july-mixed-in-august",
                            reason="后续开放月整体更正组合业务",
                            posting_date=date(2026, 8, 1),
                        )
                    )
                assert reversal.status == "posted", reversal.errors
                session.commit()
                close = session.get(AccountingPeriodClose, close_id)
                assert close.calculation_hash == original_hash
                assert close.calculation_payload == original_payload
        finally:
            authority_stack.close()
            engine.dispose()


def test_postgres_close_vs_close_is_linearized() -> None:
    with isolated_postgres_url("finance_company") as database_url:
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        authority_stack = ExitStack()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG并发月结",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(
                        session,
                        organization,
                        registry_database_name=engine.url.database,
                    )
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="8" * 64,
                    original_name="concurrent.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/concurrent.txt",
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    session.add(evidence)
                    session.flush()
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            with Session(engine) as session:
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
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
                prepare_authenticated_bank_account(
                    session,
                    organization,
                    booking_date=date(2026, 7, 1),
                    authority=authority,
                    evidence_id=evidence_id,
                    accounts=[],
                )
                _confirm_partial_year_zero_opening(
                    session,
                    authority,
                    org_id=org_id,
                    evidence_id=evidence_id,
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
                                management_commentary_context_hash=preview.data[
                                    "assistant_review_checklist"
                                ]["management_commentary"]["context_hash"],
                                management_commentary="七月经营情况已基于关账上下文完成分析。",
                                owner_approval_id=owner_approval_id,
                                idempotency_key=key,
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    payroll_settlements_reviewed=True,
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
            authority_stack.close()
            engine.dispose()


def test_postgres_close_vs_post_is_linearized() -> None:
    with isolated_postgres_url("finance_company") as database_url:
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        authority_stack = ExitStack()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG入账月结并发",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(
                        session,
                        organization,
                        registry_database_name=engine.url.database,
                    )
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="9" * 64,
                    original_name="post-close.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/post-close.txt",
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    session.add(evidence)
                    session.flush()
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            with Session(engine) as session:
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
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
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    baseline = FinanceService(session).record_event(
                        _sale(
                            org_id,
                            "pg-post-close-baseline",
                            evidence_id=evidence_id,
                        )
                    )
                assert baseline.status == "posted"
                session.commit()
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
                _confirm_partial_year_zero_opening(
                    session,
                    authority,
                    org_id=org_id,
                    evidence_id=evidence_id,
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
                                management_commentary_context_hash=preview.data[
                                    "assistant_review_checklist"
                                ]["management_commentary"]["context_hash"],
                                management_commentary="七月经营情况已基于关账上下文完成分析。",
                                owner_approval_id=owner_approval_id,
                                idempotency_key="pg-post-close-close",
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    payroll_settlements_reviewed=True,
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
                            _sale(
                                org_id,
                                "pg-post-close-racing-post",
                                evidence_id=evidence_id,
                            )
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
            authority_stack.close()
            engine.dispose()


def test_postgres_period_generation_concurrency_and_payload_identity() -> None:
    with isolated_postgres_url("finance_company") as database_url:
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        authority_stack = ExitStack()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG期间生成并发",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(
                        session,
                        organization,
                        registry_database_name=engine.url.database,
                    )
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="b" * 64,
                    original_name="generation.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/generation.txt",
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    session.add(evidence)
                    session.flush()
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            barrier = Barrier(2)

            def generate() -> tuple[str, object, object, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    with authority.attributed_call(
                        session, tool_name="finance_generate_accounting_period"
                    ):
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
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
                    mismatch = service.generate_accounting_period(
                        GenerateAccountingPeriodRequest(
                            org_id=org_id,
                            period_month="2026-04",
                            idempotency_key="pg-generation-same-key",
                            confirmation_note="并发同载荷生成",
                            evidence_references=[evidence_id],
                        )
                    )
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
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

            with pytest.raises(DBAPIError, match="ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE"):
                with Session(engine) as session:
                    with authority.attributed_call(session, tool_name="finance_negative_tamper"):
                        session.execute(
                            sa.text(
                                "UPDATE accounting_period_actions "
                                "SET input_facts = input_facts::jsonb || "
                                '\'{"secret":"forbidden"}\'::jsonb WHERE id = :id'
                            ),
                            {"id": results[0][1]},
                        )
                        session.commit()

        finally:
            authority_stack.close()
            engine.dispose()


def test_postgres_owner_close_vs_raw_payroll_is_linearized() -> None:
    """Keep the owner-mode close race separate from legacy multi-org guards."""

    with isolated_postgres_url("finance_company") as database_url:
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        authority_stack = ExitStack()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG owner月结工资并发",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(
                        session,
                        organization,
                        registry_database_name=engine.url.database,
                    )
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
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    session.add(evidence)
                    session.flush()
                session.commit()
                org_id, evidence_id = organization.id, evidence.id
            policy_id = uuid.uuid4()
            with Session(engine) as session:
                with authority.attributed_call(
                    session, tool_name="finance_register_payroll_policy"
                ) as attribution:
                    session.execute(
                        sa.text(
                            """
                        INSERT INTO payroll_policy_versions (
                            id, org_id, region, supersedes_id, effective_from,
                            effective_to, version, source_url, parameters,
                            execution_attribution_id, created_at
                        ) VALUES (
                            :id, :org_id, 'owner并发门禁', NULL, '2026-01-01',
                            '2026-12-31', 'owner-concurrent-v1',
                            'https://www.chinatax.gov.cn/', '{}'::jsonb,
                            :attribution_id, CURRENT_TIMESTAMP
                        )
                        """
                        ),
                        {
                            "id": policy_id,
                            "org_id": org_id,
                            "attribution_id": attribution.id,
                        },
                    )
                session.commit()
            with Session(engine) as session:
                with authority.attributed_call(
                    session, tool_name="finance_generate_accounting_period"
                ):
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
                prepare_authenticated_bank_account(
                    session,
                    organization,
                    booking_date=date(2026, 7, 1),
                    authority=authority,
                    evidence_id=evidence_id,
                    accounts=[],
                )
                _confirm_partial_year_zero_opening(
                    session,
                    authority,
                    org_id=org_id,
                    evidence_id=evidence_id,
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
                                management_commentary_context_hash=preview.data[
                                    "assistant_review_checklist"
                                ]["management_commentary"]["context_hash"],
                                management_commentary="本月经营情况已基于关账上下文完成分析。",
                                owner_approval_id=owner_approval_id,
                                idempotency_key="owner-close-payroll-close",
                                review_facts=AccountingPeriodReviewFacts(
                                    voucher_completeness_reviewed=True,
                                    bank_reconciliation_reviewed=True,
                                    open_items_reviewed=True,
                                    payroll_and_statutory_items_reviewed=True,
                                    payroll_settlements_reviewed=True,
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
            authority_stack.close()
            engine.dispose()


def test_postgres_payroll_dependency_and_generation_writes_are_serialized() -> None:
    """A company lock serializes competing first-period generation requests.

    Payroll-close serialization remains covered by the owner close-vs-payroll
    test. Typed source/dependency conservation is covered by the PostgreSQL
    component lifecycle suite without disabling triggers.
    """

    with isolated_postgres_url("finance_company") as database_url:
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="PG 跨月生成并发",
                )
                session.commit()
                with catalog_owner_authority(
                    session,
                    organization,
                    registry_database_name=engine.url.database,
                ) as authority:
                    with authority.attributed_call(session, tool_name="finance_register_evidence"):
                        evidence = Evidence(
                            org_id=organization.id,
                            sha256="e" * 64,
                            original_name="generation-concurrency.txt",
                            media_type="text/plain",
                            source="test",
                            size_bytes=1,
                            storage_path="test/generation-concurrency.txt",
                        )
                        session.add(evidence)
                        session.flush()
                    session.commit()
                    org_id, evidence_id = organization.id, evidence.id
                    barrier = Barrier(2)

                    def generate(month: str) -> tuple[str, list[str]]:
                        with Session(engine) as concurrent_session:
                            barrier.wait()
                            with authority.attributed_call(
                                concurrent_session,
                                tool_name="finance_generate_accounting_period",
                            ):
                                result = AccountingPeriodService(
                                    concurrent_session,
                                    current_date=date(2026, 8, 11),
                                ).generate_accounting_period(
                                    GenerateAccountingPeriodRequest(
                                        org_id=org_id,
                                        period_month=month,
                                        idempotency_key=f"org-lock-{month}",
                                        confirmation_note="跨月并发",
                                        evidence_references=[evidence_id],
                                    )
                                )
                            concurrent_session.commit()
                            return str(result.status), result.errors

                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(executor.map(generate, ("2026-05", "2026-07")))
                    assert [status for status, _ in results].count("posted") == 1
                    assert [errors for status, errors in results if status == "rejected"] == [
                        ["ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE"]
                    ]
                    assert (
                        session.scalar(sa.select(sa.func.count()).select_from(AccountingPeriod))
                        == 1
                    )
        finally:
            engine.dispose()
