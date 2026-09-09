from __future__ import annotations

import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import catalog_owner_authority
from alembic.config import Config
from conftest import AuthenticatedOwnerAuthority
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.borrowing_schemas import PreviewBorrowingInterestRequest
from ai_accounting.borrowing_service import BorrowingService
from ai_accounting.coa import get_account_by_role, seed_organization
from ai_accounting.component_schemas import ConfigureAccountRequest, RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    Borrowing,
    BorrowingPayment,
    BusinessEvent,
    BusinessEventComponent,
    ComponentCashFlowAllocation,
    Evidence,
    OpenItem,
    Voucher,
    VoucherLine,
    event_evidence,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]
IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"


@contextmanager
def _postgres_url():
    external = os.environ.get("FINANCE_COMPONENT_GUARD_TEST_URL")
    if external:
        yield external
        return
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    with PostgresContainer(IMAGE, driver="psycopg") as postgres:
        yield postgres.get_connection_url(driver="psycopg")


@pytest.fixture(scope="module")
def component_database():
    with _postgres_url() as url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        engine = sa.create_engine(url)
        try:
            with Session(engine) as session:
                org_id = uuid.uuid4()
                org = seed_organization(
                    session,
                    name="Component PostgreSQL guard",
                    taxpayer_identification_number="91330106MA1234567T",
                    org_id=org_id,
                    accounting_period_control_enabled=False,
                )
                session.commit()
                with catalog_owner_authority(session, org) as authority:
                    evidence = Evidence(
                        org_id=org.id,
                        sha256=uuid.uuid4().hex * 2,
                        original_name="component-guard.txt",
                        media_type="text/plain",
                        source="postgres-component-test",
                        size_bytes=1,
                        storage_path=f"tests/{org.id}/component-guard.txt",
                        metadata_json={},
                    )
                    with authority.attributed_call(
                        session, tool_name="finance_component_guard_evidence"
                    ):
                        session.add(evidence)
                        session.flush()
                    session.commit()
                    identity = (org.id, evidence.id, authority)
                    yield engine, identity
        finally:
            engine.dispose()


def _expense(key: str, amount: int, expense_class: str = "general_expense", **extra):
    return {
        "key": key,
        "kind": "expense",
        "business_date": "2026-03-05",
        "payment_date": "2026-03-05",
        "amount_fen": amount,
        "expense_class": expense_class,
        "payment_basis": "immediate",
        **extra,
    }


def _request(org_id, evidence_id, components, *, key: str, allocations=None):
    allocations = allocations or [
        {"component_key": component["key"], "amount_fen": component["amount_fen"]}
        for component in components
    ]
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-03-05",
            "evidence_references": [evidence_id],
            "components": components,
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": sum(item["amount_fen"] for item in allocations),
                    "allocations": allocations,
                }
            ],
        }
    )


def test_authenticated_components_cover_composition_idempotency_and_rollback(
    component_database,
):
    engine, (org_id, evidence_id, authority) = component_database
    assert isinstance(authority, AuthenticatedOwnerAuthority)
    with Session(engine) as session:
        service = ComponentService(session)
        with authority.attributed_call(session, tool_name="finance_configure_account"):
            configured = service.configure_account(
                ConfigureAccountRequest(
                    org_id=org_id,
                    idempotency_key="component-travel-detail",
                    code="560209",
                    name="差旅费",
                    business_class="general_expense",
                )
            )
        request = _request(
            org_id,
            evidence_id,
            [
                _expense("travel", 101, account_code="560209"),
                _expense("selling", 202, "sales_expense"),
            ],
            key="postgres-two-expenses",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            posted = service.record(request)
        assert posted.status == "posted", posted
        session.commit()
        assert configured["code"] == "560209"
        event_id = posted.event_id
        with authority.attributed_call(session, tool_name="finance_record_event"):
            duplicate = service.record(request)
        assert duplicate.event_id == event_id
        assert (
            session.scalar(
                sa.select(sa.func.count()).select_from(Voucher).where(Voucher.event_id == event_id)
            )
            == 1
        )
        flows = session.execute(
            sa.select(BusinessEventComponent.key, ComponentCashFlowAllocation.amount_fen)
            .join(
                ComponentCashFlowAllocation,
                ComponentCashFlowAllocation.component_id == BusinessEventComponent.id,
            )
            .where(BusinessEventComponent.event_id == event_id)
        ).all()
        assert sorted(flows) == [("selling", -202), ("travel", -101)]

        party = {"kind": "supplier", "name": "Local source supplier"}
        local = _request(
            org_id,
            evidence_id,
            [
                _expense(
                    "purchase",
                    300,
                    payment_basis="supplier_credit",
                    metadata={"counterparty": party},
                ),
                {
                    "key": "settle",
                    "kind": "payable_settlement",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "allocations": [{"source_component_key": "purchase", "amount_fen": 300}],
                    "metadata": {"counterparty": party},
                },
            ],
            key="postgres-local-create-settle",
            allocations=[{"component_key": "settle", "amount_fen": 300}],
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            settled = service.record(local)
        assert settled.status == "posted", settled
        session.commit()
        item = session.scalar(
            sa.select(OpenItem).where(OpenItem.source_event_id == settled.event_id)
        )
        assert item is not None and item.status == "settled"

        second_payment = _request(
            org_id,
            evidence_id,
            [
                {
                    "key": "duplicate",
                    "kind": "payable_settlement",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "allocations": [{"open_item_id": str(item.id), "amount_fen": 300}],
                    "metadata": {"counterparty": party},
                }
            ],
            key="postgres-duplicate-settlement",
            allocations=[{"component_key": "duplicate", "amount_fen": 300}],
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            rejected = service.record(second_payment)
        assert rejected.status == "rejected"
        assert any("SETTLEMENT" in error or "OPEN_ITEM" in error for error in rejected.errors)

        before = session.scalar(sa.select(sa.func.count()).select_from(BusinessEvent))
        invalid = _request(
            org_id,
            evidence_id,
            [_expense("missing", 99, expense_class=None)],
            key="postgres-rollback-all",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            rolled_back = service.record(invalid)
        assert rolled_back.status == "needs_information"
        assert session.scalar(sa.select(sa.func.count()).select_from(BusinessEvent)) == before

        component = session.scalar(
            sa.select(BusinessEventComponent).where(BusinessEventComponent.event_id == event_id)
        )
        component.derived = dict(component.derived) | {"tampered": True}
        with pytest.raises(DBAPIError, match="FINAL_COMPONENT_IMMUTABLE"):
            session.commit()
        session.rollback()

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reversed_result = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=org_id,
                    event_id=event_id,
                    posting_date="2026-03-06",
                    idempotency_key="postgres-reverse-components",
                    reason="PostgreSQL inverse provenance test",
                )
            )
        assert reversed_result.status == "posted", reversed_result
        session.commit()
        reversal_components = session.scalars(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == reversed_result.event_id
            )
        ).all()
        assert reversal_components
        assert {component.kind for component in reversal_components} == {"reversal"}


def test_database_rejects_forged_domain_component_with_arbitrary_lines(component_database):
    engine, (org_id, evidence_id, authority) = component_database
    with Session(engine) as session:
        with authority.attributed_call(session, tool_name="finance_forged_direct_write"):
            event = BusinessEvent(
                org_id=org_id,
                idempotency_key="forged-domain-component",
                request_payload_hash="f" * 64,
                event_type="composite",
                status="draft",
                facts={},
                business_date=date(2026, 3, 5),
                posting_date=date(2026, 3, 5),
                rule_trace=[],
            )
            session.add(event)
            session.flush()
            session.execute(
                event_evidence.insert().values(
                    org_id=org_id,
                    event_id=event.id,
                    evidence_id=evidence_id,
                    relation_kind="supporting",
                )
            )
            component = BusinessEventComponent(
                org_id=org_id,
                event_id=event.id,
                key="fake-asset",
                ordinal=1,
                kind="fixed_asset_acquisition",
                facts={
                    "asset_code": "FORGED",
                    "cost_components": {"purchase_price_fen": 100},
                },
                derived={},
            )
            session.add(component)
            session.flush()
            voucher = Voucher(
                org_id=org_id,
                event_id=event.id,
                voucher_number="202603-9999",
                posting_date=event.posting_date,
                description="forged arbitrary lines",
                status="draft",
            )
            session.add(voucher)
            session.flush()
            expense = get_account_by_role(session, org_id, "general_expense")
            cash = get_account_by_role(session, org_id, "cash")
            rows = [(expense, 100, 0), (cash, 0, 100)]
            session.add_all(
                [
                    VoucherLine(
                        org_id=org_id,
                        voucher_id=voucher.id,
                        component_id=component.id,
                        line_number=index,
                        account_id=account.id,
                        debit_fen=debit,
                        credit_fen=credit,
                    )
                    for index, (account, debit, credit) in enumerate(rows, 1)
                ]
            )
            component.derived = {
                "_posting_entries": [
                    {
                        "account_id": str(account.id),
                        "counterparty_id": None,
                        "debit_fen": debit,
                        "credit_fen": credit,
                    }
                    for account, debit, credit in rows
                ],
                "_posting_open_items": [],
                "_posting_settlements": [],
                "_posting_cash_flows": [],
            }
            session.flush()
            voucher.status = "posted"
            event.status = "posted"
            with pytest.raises(DBAPIError, match="FIXED_ASSET_COMPONENT_FACTS_MISMATCH"):
                session.commit()


def test_same_event_borrowing_accrual_interest_and_principal_is_guarded_by_origins(
    component_database,
):
    engine, (org_id, evidence_id, authority) = component_database
    draw_day, due_day = date(2026, 4, 1), date(2026, 5, 1)
    draw = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "postgres-borrowing-draw",
            "posting_date": draw_day,
            "evidence_references": [evidence_id],
            "components": [
                {
                    "key": "loan",
                    "kind": "borrowing_drawdown",
                    "business_date": draw_day,
                    "facts": {
                        "lender": {"name": "Component bank"},
                        "lender_is_licensed_financial_institution": True,
                        "currency": "CNY",
                        "principal_fen": 1000000,
                        "annual_rate_percent": "3.65",
                        "day_count_basis": "actual_365",
                        "capitalization_applicable": False,
                        "due_date": due_day,
                        "term_facts": {
                            "single_drawdown": True,
                            "fixed_rate": True,
                            "simple_interest": True,
                            "bullet_principal_at_maturity": True,
                            "allows_prepayment": False,
                            "allows_extension": False,
                            "has_penalty_interest": False,
                            "has_financing_fees": False,
                        },
                    },
                    "metadata": {
                        "borrowing_code": "PG-LOAN-COMPONENT",
                        "contract_name": "PostgreSQL component loan",
                        "interest_due_dates": [due_day],
                        "purpose": "working capital",
                    },
                }
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": draw_day,
                    "amount_fen": 1000000,
                    "allocations": [{"component_key": "loan", "amount_fen": 1000000}],
                }
            ],
        }
    )
    with Session(engine) as session:
        service = ComponentService(session)
        with authority.attributed_call(session, tool_name="finance_record_event"):
            posted = service.record(draw)
        assert posted.status == "posted", posted
        session.commit()
        borrowing = session.scalar(
            sa.select(Borrowing).where(Borrowing.drawdown_event_id == posted.event_id)
        )
        preview = BorrowingService(session).preview_borrowing_interest(
            PreviewBorrowingInterestRequest(
                org_id=org_id,
                borrowing_id=borrowing.id,
                period_start=draw_day,
                period_end=due_day,
            )
        )
        interest_fen = preview.data["interest_fen"]
        lifecycle = RecordEventRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": "postgres-borrowing-close",
                "posting_date": due_day,
                "evidence_references": [evidence_id],
                "components": [
                    {
                        "key": "principal",
                        "kind": "borrowing_principal_repayment",
                        "business_date": due_day,
                        "payment_date": due_day,
                        "borrowing_id": borrowing.id,
                        "amount_fen": borrowing.principal_fen,
                    },
                    {
                        "key": "interest-payment",
                        "kind": "borrowing_interest_payment",
                        "business_date": due_day,
                        "payment_date": due_day,
                        "borrowing_id": borrowing.id,
                        "accrual_component_key": "accrual",
                        "amount_fen": interest_fen,
                    },
                    {
                        "key": "accrual",
                        "kind": "borrowing_interest_accrual",
                        "business_date": due_day,
                        "facts": {
                            "borrowing_id": borrowing.id,
                            "period_start": draw_day,
                            "period_end": due_day,
                            "calculation_hash": preview.calculation_hash,
                        },
                    },
                ],
                "funds": [
                    {
                        "key": "cash",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": due_day,
                        "amount_fen": borrowing.principal_fen + interest_fen,
                        "allocations": [
                            {"component_key": "principal", "amount_fen": borrowing.principal_fen},
                            {"component_key": "interest-payment", "amount_fen": interest_fen},
                        ],
                    }
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            closed = service.record(lifecycle)
        assert closed.status == "posted", closed
        session.commit()
        payments = session.scalars(
            sa.select(BorrowingPayment).where(BorrowingPayment.event_id == closed.event_id)
        ).all()
        assert {payment.payment_kind for payment in payments} == {"interest", "principal"}
        assert {payment.component_id for payment in payments} == {
            component.id
            for component in session.scalars(
                sa.select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == closed.event_id,
                    BusinessEventComponent.kind.in_(
                        ["borrowing_interest_payment", "borrowing_principal_repayment"]
                    ),
                )
            )
        }
