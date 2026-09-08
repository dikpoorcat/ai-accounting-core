"""PostgreSQL commit-boundary attacks for the R3 provenance invariants."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from collections.abc import Iterator
from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from conftest import prepare_authenticated_bank_account
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from test_payroll_service import add_bank_row, payment_request

from ai_accounting.models import (
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollEventLink,
    PayrollPolicyVersion,
)
from ai_accounting.service import FinanceService

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture
def postgres_engine() -> Iterator[object]:
    with authenticated_business_database("round3_lineage") as database:
        yield database


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
    session: Session, organization: object, authority, evidence_id
) -> tuple[object, list[OpenItem]]:
    """Return a final statutory payment and the three open items it settled."""

    prepare_authenticated_bank_account(
        session, organization, authority=authority, evidence_id=evidence_id
    )
    service = FinanceService(session)
    _org, _batch, _line, _proof, source_event = confirmed_payroll(
        session, organization.id, evidence_id, authority, key="r3-pg-source"
    )
    salary_item = session.scalar(
        sa.select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == source_event.id,
            OpenItem.payable_category == "salary",
        )
    )
    assert salary_item is not None
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
        bank=add_bank_row(
            session,
            organization,
            -sum(item.original_amount_fen for item in statutory_items),
            "r3-pg-source-statutory-bank",
        ),
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

    postgres_engine, org_id, original_id, authority = postgres_engine
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        with authority.attributed_call(
            session, tool_name="finance_register_payroll_policy_version"
        ):
            policy = _policy(session, organization.id, "evidence-freeze")
        original = session.get(Evidence, original_id)
        with authority.attributed_call(session, tool_name="finance_register_evidence"):
            replacement = _evidence(session, organization.id, "r3-replacement-evidence")
        with authority.attributed_call(session, tool_name="finance_preview_payroll"):
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

    postgres_engine, org_id, evidence_id, authority = postgres_engine
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        statutory, statutory_items = _post_two_partial_salary_social_payment(
            session, organization, authority, evidence_id
        )
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

    postgres_engine, org_id, evidence_id, authority = postgres_engine
    with authenticated_business_database("round3_foreign") as foreign_database:
        _foreign_engine, foreign_org_id, foreign_evidence_id, _foreign_authority = foreign_database
        assert foreign_org_id != org_id
        with Session(postgres_engine) as session:
            with authority.attributed_call(
                session, tool_name="finance_register_payroll_policy_version"
            ):
                policy = _policy(session, org_id, "cross-org")
            batch = PayrollBatch(
                org_id=org_id,
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
            with authority.attributed_call(session, tool_name="finance_preview_payroll"):
                session.add(batch)
            session.commit()
            batch_id = batch.id
            with pytest.raises(IntegrityError, match="fk_payroll_batch_evidence_org_evidence"):
                session.execute(
                    sa.text(
                        "INSERT INTO payroll_batch_evidence "
                        "(org_id, payroll_batch_id, evidence_id, created_at) "
                        "VALUES (:org_id, :batch_id, :evidence_id, CURRENT_TIMESTAMP)"
                    ),
                    {"org_id": org_id, "batch_id": batch_id, "evidence_id": foreign_evidence_id},
                )
                session.commit()
            session.rollback()
            assert not session.scalars(
                sa.select(PayrollBatchEvidence).where(
                    PayrollBatchEvidence.payroll_batch_id == batch_id
                )
            ).all()
            # The same draft accepts its own registered evidence and can be sealed.
            with authority.attributed_call(session, tool_name="finance_preview_payroll"):
                batch = session.get(PayrollBatch, batch_id)
                session.add(
                    PayrollBatchEvidence(
                        org_id=org_id, payroll_batch_id=batch_id, evidence_id=evidence_id
                    )
                )
                session.flush()
                batch.status = "calculated"
            session.commit()
            assert batch.status == "calculated"
