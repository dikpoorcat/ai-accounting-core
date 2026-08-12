"""PostgreSQL commit-boundary attacks for the R3 provenance invariants."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from collections.abc import Iterator
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.config import Config
from conftest import authenticate_and_confirm_bank_scope
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_payroll_service import payment_request, register_payroll_facts
from test_round3_lineage import _preview
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    Evidence,
    OpenItem,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollEventLink,
    PayrollPolicyVersion,
)
from ai_accounting.schemas import ConfirmPayrollRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def _policy(session: Session, org_id: object, key: str) -> PayrollPolicyVersion:
    policy = PayrollPolicyVersion(
        org_id=org_id,
        region="CN-310000",
        effective_from=date(2025, 7, 1),
        version=f"r3-lineage-{key}",
        source_url="https://www.chinatax.gov.cn/",
        parameters={"r3": key},
    )
    session.add(policy)
    session.flush()
    return policy


def _evidence(session: Session, org_id: object, key: str) -> Evidence:
    evidence = Evidence(
        org_id=org_id,
        sha256=hashlib.sha256(key.encode("utf-8")).hexdigest(),
        original_name=f"{key}.txt",
        media_type="text/plain",
        source="r3-postgres-test",
        size_bytes=1,
        storage_path=f"/r3/{key}.txt",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()
    return evidence


def _sealed_batch(
    session: Session,
    org_id: object,
    policy: PayrollPolicyVersion,
    evidence: Evidence,
    key: str,
) -> PayrollBatch:
    """Use the only legal construction sequence: draft edge, then seal it."""

    batch = PayrollBatch(
        org_id=org_id,
        idempotency_key=f"r3-pbe-{key}",
        batch_kind="regular",
        payroll_period="2026-03",
        version=1,
        status="draft",
        calculation_hash=(key * 64)[:64],
        request_payload_hash=("r" + key * 63)[:64],
        calculation_input={"request": {"evidence_references": [str(evidence.id)]}},
        calculation_trace=[],
        policy_snapshot={"version": policy.version},
        policy_version_id=policy.id,
        posting_date=date(2026, 3, 5),
        payment_date=date(2026, 3, 5),
    )
    session.add(batch)
    session.flush()
    session.add(
        PayrollBatchEvidence(
            org_id=org_id,
            payroll_batch_id=batch.id,
            evidence_id=evidence.id,
        )
    )
    session.flush()
    batch.status = "calculated"
    session.flush()
    return batch


def _post_two_partial_salary_social_payment(
    session: Session, organization: object
) -> tuple[object, list[OpenItem]]:
    """Return a final statutory payment and the three open items it settled."""

    service = FinanceService(session)
    employee_id = register_payroll_facts(session, organization)
    preview = _preview(
        service, organization.id, employee_id, idempotency_key="r3-pg-source-preview"
    )
    assert preview.status == "calculated", preview.errors
    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="r3-pg-source-confirm",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    salary_item = session.scalar(
        sa.select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert salary_item is not None
    scope_evidence = _evidence(session, organization.id, "r3-pg-bank-scope")
    authority = authenticate_and_confirm_bank_scope(
        session,
        organization,
        evidence_id=scope_evidence.id,
        accounts=[
            {
                "bank_account_code": "1002",
                "account_name": "银行存款",
                "start_date": date(2026, 3, 1),
            }
        ],
    )

    for key, cash, tax in (("one", 425_000, 0), ("two", 414_500, 10_500)):
        request = payment_request(
            organization,
            event_type="salary_payment",
            amount_fen=cash,
            allocations=[{"open_item_id": salary_item.id, "amount_fen": 500_000}],
            salary_withholdings=[
                {
                    "open_item_id": salary_item.id,
                    "employee_social_insurance_items": {"pension": 40_000},
                    "employee_housing_fund_items": {"housing_fund": 35_000},
                    "individual_income_tax_fen": tax,
                }
            ],
            bank=None,
            key=f"r3-pg-source-salary-{key}",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            payment = service.record_event(request)
        assert payment.status == "posted", payment.errors

    statutory_items = session.scalars(
        sa.select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.payable_category.in_(("employer_social", "withheld_employee_social")),
        )
    ).all()
    assert len(statutory_items) == 3
    request = payment_request(
        organization,
        event_type="social_insurance_payment",
        amount_fen=sum(item.original_amount_fen for item in statutory_items),
        allocations=[
            {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
            for item in statutory_items
        ],
        bank=None,
        key="r3-pg-source-statutory",
    )
    with authority.attributed_call(session, tool_name="finance_record_event"):
        statutory = service.record_event(request)
    assert statutory.status == "posted", statutory.errors
    return statutory, statutory_items


def test_r3_007_postgresql_sealed_payroll_evidence_rejects_sql_mutations(
    postgres_engine: object,
) -> None:
    """INSERT, UPDATE and DELETE against a sealed evidence set all fail in PostgreSQL."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-007 evidence freeze"
        )
        policy = _policy(session, organization.id, "evidence-freeze")
        original = _evidence(session, organization.id, "r3-original-evidence")
        replacement = _evidence(session, organization.id, "r3-replacement-evidence")
        batch = _sealed_batch(session, organization.id, policy, original, "evidence-freeze")
        organization_id = organization.id
        batch_id = batch.id
        original_id = original.id
        replacement_id = replacement.id
        session.commit()

    mutations = (
        sa.text(
            "DELETE FROM payroll_batch_evidence "
            "WHERE org_id = :org_id AND payroll_batch_id = :batch_id AND evidence_id = :original_id"
        ),
        sa.text(
            "UPDATE payroll_batch_evidence SET evidence_id = :replacement_id "
            "WHERE org_id = :org_id AND payroll_batch_id = :batch_id AND evidence_id = :original_id"
        ),
        sa.text(
            "INSERT INTO payroll_batch_evidence "
            "(org_id, payroll_batch_id, evidence_id, created_at) "
            "VALUES (:org_id, :batch_id, :replacement_id, CURRENT_TIMESTAMP)"
        ),
    )
    parameters = {
        "org_id": organization_id,
        "batch_id": batch_id,
        "original_id": original_id,
        "replacement_id": replacement_id,
    }
    for mutation in mutations:
        with Session(postgres_engine) as session:
            with pytest.raises(DBAPIError, match="evidence is immutable once the draft is sealed"):
                session.execute(mutation, parameters)
                session.flush()
            session.rollback()

    with Session(postgres_engine) as session:
        evidence_ids = session.scalars(
            sa.select(PayrollBatchEvidence.evidence_id).where(
                PayrollBatchEvidence.org_id == organization_id,
                PayrollBatchEvidence.payroll_batch_id == batch_id,
            )
        ).all()
        assert evidence_ids == [original_id]


def test_r3_006_postgresql_source_edges_are_complete_and_immutable(
    postgres_engine: object,
) -> None:
    """A direct SQL attack cannot erase, retarget or append a final source edge."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-006 source edges"
        )
        statutory, statutory_items = _post_two_partial_salary_social_payment(session, organization)
        organization_id = organization.id
        statutory_event_id = statutory.event_id
        statutory_item_ids = {item.id for item in statutory_items}
        session.commit()

    with Session(postgres_engine) as session:
        source_edges = session.scalars(
            sa.select(PayrollEventLink)
            .where(
                PayrollEventLink.org_id == organization_id,
                PayrollEventLink.event_id == statutory_event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
            .order_by(PayrollEventLink.id)
        ).all()
        assert len(source_edges) == 3
        assert all(edge.source_payment_event_id is not None for edge in source_edges)
        assert {edge.source_open_item_id for edge in source_edges} == statutory_item_ids
        edge_id = source_edges[0].id
        batch_id = source_edges[0].payroll_batch_id
        source_event_id = source_edges[0].source_payment_event_id
        source_item_id = source_edges[0].source_open_item_id
        source_edge_ids = [edge.id for edge in source_edges]

    parameters = {
        "org_id": organization_id,
        "event_id": statutory_event_id,
        "edge_id": edge_id,
        "batch_id": batch_id,
        "source_event_id": source_event_id,
        "source_item_id": source_item_id,
        "new_id": uuid.uuid4(),
    }
    mutations = (
        sa.text("DELETE FROM payroll_event_links WHERE id = :edge_id"),
        sa.text(
            "UPDATE payroll_event_links SET source_payment_event_id = NULL WHERE id = :edge_id"
        ),
        sa.text(
            "INSERT INTO payroll_event_links "
            "(id, org_id, event_id, payroll_batch_id, source_payment_event_id, "
            "source_open_item_id, link_kind, created_at) "
            "VALUES (:new_id, :org_id, :event_id, :batch_id, :source_event_id, "
            ":source_item_id, 'statutory_payment', CURRENT_TIMESTAMP)"
        ),
    )
    for mutation in mutations:
        with Session(postgres_engine) as session:
            with pytest.raises(DBAPIError, match="payroll event links are immutable"):
                session.execute(mutation, parameters)
                session.flush()
            session.rollback()

    with Session(postgres_engine) as session:
        assert (
            session.scalars(
                sa.select(PayrollEventLink.id)
                .where(
                    PayrollEventLink.org_id == organization_id,
                    PayrollEventLink.event_id == statutory_event_id,
                    PayrollEventLink.link_kind == "statutory_payment",
                )
                .order_by(PayrollEventLink.id)
            ).all()
            == source_edge_ids
        )


def test_r3_007_postgresql_rejects_cross_organization_draft_evidence(
    postgres_engine: object,
) -> None:
    """Draft mutability never weakens the composite organization evidence FK."""

    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-007 evidence A"
        )
        organization_b = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-007 evidence B"
        )
        policy = _policy(session, organization_a.id, "cross-org")
        foreign_evidence = _evidence(session, organization_b.id, "r3-foreign-evidence")
        batch = PayrollBatch(
            org_id=organization_a.id,
            idempotency_key="r3-pbe-cross-org",
            batch_kind="regular",
            payroll_period="2026-04",
            version=1,
            status="draft",
            calculation_hash="c" * 64,
            request_payload_hash="d" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy.id,
            posting_date=date(2026, 4, 5),
            payment_date=date(2026, 4, 5),
        )
        session.add(batch)
        session.flush()
        with pytest.raises(IntegrityError):
            session.execute(
                sa.text(
                    "INSERT INTO payroll_batch_evidence "
                    "(org_id, payroll_batch_id, evidence_id, created_at) "
                    "VALUES (:org_id, :batch_id, :evidence_id, CURRENT_TIMESTAMP)"
                ),
                {
                    "org_id": organization_a.id,
                    "batch_id": batch.id,
                    "evidence_id": foreign_evidence.id,
                },
            )
            session.flush()
        session.rollback()

        # The rollback proves that the rejected edge did not leave a partial
        # relation; construct an independent legal draft in a fresh transaction.
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-007 evidence legal"
        )
        policy = _policy(session, organization.id, "legal-draft")
        evidence = _evidence(session, organization.id, "r3-legal-evidence")
        batch = _sealed_batch(session, organization.id, policy, evidence, "legal-draft")
        session.commit()
        assert batch.status == "calculated"
