"""Commit-boundary PostgreSQL attacks for R4 event, evidence, and source edges."""

from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from component_posting_helpers import create_component_voucher as create_voucher
from conftest import prepare_authenticated_bank_account
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_payroll_service import add_bank_row, payment_request
from test_round3_lineage_postgres import _post_two_partial_salary_social_payment

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.ledger import (
    CashFlowPlan,
    ComponentPostingPlan,
    Entry,
    SettlementPlan,
    commit_posting_plan,
)
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollBatchEvidence,
    PayrollEventLink,
    PayrollLine,
    PayrollWithholdingEntitlement,
    Voucher,
    VoucherLine,
    event_evidence,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture
def postgres_database():
    with authenticated_business_database(
        "round4_event_integrity", name="R4 事件完整性"
    ) as database:
        yield database


def _confirmed_payroll_with_evidence(
    session: Session,
    org_id,
    evidence_id,
    authority,
    *,
    key: str,
) -> tuple[object, PayrollBatch, PayrollLine, Evidence, BusinessEvent]:
    return confirmed_payroll(
        session,
        org_id,
        evidence_id,
        authority,
        key=f"r4-{key}",
    )


def _draft_batch_and_event(
    session: Session,
    authority,
    batch: PayrollBatch,
    line: PayrollLine,
    *,
    key: str,
    payroll_period: str = "2026-04",
    version: int = 1,
) -> tuple[PayrollBatch, PayrollLine, BusinessEvent]:
    """Create ordinary draft parents that must never receive final facts."""

    with authority.attributed_call(session, tool_name="finance_test_draft_parent") as attribution:
        draft_batch = PayrollBatch(
            org_id=batch.org_id,
            idempotency_key=f"r4-draft-batch-{key}",
            batch_kind="regular",
            payroll_period=payroll_period,
            version=version,
            status="draft",
            calculation_hash=(f"r4-{key}" * 64)[:64],
            request_payload_hash=(f"r4-request-{key}" * 64)[:64],
            calculation_input={},
            calculation_trace=[],
            policy_snapshot=batch.policy_snapshot,
            policy_version_id=batch.policy_version_id,
            posting_date=date.fromisoformat(f"{payroll_period}-05"),
            payment_date=date.fromisoformat(f"{payroll_period}-05"),
            execution_attribution_id=attribution.id,
        )
        session.add(draft_batch)
        session.flush()
        draft_line = PayrollLine(
            org_id=batch.org_id,
            payroll_batch_id=draft_batch.id,
            employee_id=line.employee_id,
            employee_payroll_profile_version_id=line.employee_payroll_profile_version_id,
            tax_reported_salary_fen=10_000,
            gross_salary_fen=10_000,
            net_salary_fen=10_000,
        )
        draft_event = BusinessEvent(
            org_id=batch.org_id,
            idempotency_key=f"r4-draft-event-{key}",
            event_type="expense_cash",
            status="draft",
            description="R4 草稿父对象",
            facts={},
            business_date=date.fromisoformat(f"{payroll_period}-05"),
            payment_date=date.fromisoformat(f"{payroll_period}-05"),
            posting_date=date.fromisoformat(f"{payroll_period}-05"),
            rule_trace=[],
            execution_attribution_id=attribution.id,
        )
        session.add_all([draft_line, draft_event])
        session.flush()
    return draft_batch, draft_line, draft_event


def _assert_commit_rejects(
    engine: object, authority, statement: sa.TextClause, parameters: dict[str, object]
) -> None:
    """Drive every direct-SQL attack through the real transaction commit point."""

    with Session(engine) as session:
        with pytest.raises(DBAPIError):
            with authority.attributed_call(
                session, tool_name="finance_test_round4_integrity_attack"
            ):
                session.execute(statement, parameters)
                session.commit()
        session.rollback()


def test_r4_002_final_parent_moves_reject_and_preserve_each_old_parent(
    postgres_database,
) -> None:
    """Entitlement, PEL, and evidence cannot move from a final parent to a draft."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization, batch, line, evidence, event = _confirmed_payroll_with_evidence(
            session, org_id, evidence_id, authority, key="parent-move"
        )
        entitlement = session.scalar(
            select(PayrollWithholdingEntitlement)
            .where(PayrollWithholdingEntitlement.payroll_line_id == line.id)
            .order_by(PayrollWithholdingEntitlement.id)
        )
        accrual_link = session.scalar(
            select(PayrollEventLink).where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == event.id,
                PayrollEventLink.link_kind == "payroll_accrual",
            )
        )
        assert entitlement is not None and accrual_link is not None
        draft_batch, draft_line, draft_event = _draft_batch_and_event(
            session, authority, batch, line, key="parent-move"
        )
        identifiers = {
            "org_id": organization.id,
            "final_event_id": event.id,
            "final_line_id": line.id,
            "draft_line_id": draft_line.id,
            "entitlement_id": entitlement.id,
            "link_id": accrual_link.id,
            "draft_event_id": draft_event.id,
            "final_batch_id": batch.id,
            "draft_batch_id": draft_batch.id,
            "evidence_id": evidence.id,
        }
        session.commit()

    attacks = (
        sa.text(
            "UPDATE payroll_withholding_entitlements SET payroll_line_id = :draft_line_id "
            "WHERE id = :entitlement_id AND org_id = :org_id"
        ),
        sa.text(
            "UPDATE payroll_event_links SET event_id = :draft_event_id "
            "WHERE id = :link_id AND org_id = :org_id"
        ),
        sa.text(
            "UPDATE payroll_batch_evidence SET payroll_batch_id = :draft_batch_id "
            "WHERE org_id = :org_id AND payroll_batch_id = :final_batch_id "
            "AND evidence_id = :evidence_id"
        ),
    )
    for attack in attacks:
        _assert_commit_rejects(postgres_engine, authority, attack, identifiers)

    with Session(postgres_engine) as session:
        assert (
            session.scalar(
                select(PayrollWithholdingEntitlement.payroll_line_id).where(
                    PayrollWithholdingEntitlement.id == identifiers["entitlement_id"]
                )
            )
            == identifiers["final_line_id"]
        )
        assert (
            session.scalar(
                select(PayrollEventLink.event_id).where(
                    PayrollEventLink.id == identifiers["link_id"]
                )
            )
            == identifiers["final_event_id"]
        )
        assert (
            session.scalar(
                select(PayrollBatchEvidence.evidence_id).where(
                    PayrollBatchEvidence.org_id == identifiers["org_id"],
                    PayrollBatchEvidence.payroll_batch_id == identifiers["final_batch_id"],
                )
            )
            == identifiers["evidence_id"]
        )


def test_r4_004_final_event_evidence_is_org_bound_immutable_and_inherited(
    postgres_database,
) -> None:
    """Final payroll evidence has one same-org frozen set and payroll reversals inherit it."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with authenticated_business_database("round4_foreign_evidence", name="R4 外企业证据") as (
        _foreign_engine,
        _foreign_org_id,
        foreign_evidence_id,
        _foreign_authority,
    ):
        pass
    with Session(postgres_engine) as session:
        organization, batch, _line, evidence, event = _confirmed_payroll_with_evidence(
            session, org_id, evidence_id, authority, key="event-evidence"
        )
        expected_batch_evidence = set(
            session.scalars(
                select(PayrollBatchEvidence.evidence_id).where(
                    PayrollBatchEvidence.org_id == organization.id,
                    PayrollBatchEvidence.payroll_batch_id == batch.id,
                )
            ).all()
        )
        actual_event_evidence = set(
            session.scalars(
                select(event_evidence.c.evidence_id).where(
                    event_evidence.c.org_id == organization.id,
                    event_evidence.c.event_id == event.id,
                    event_evidence.c.relation_kind == "supporting",
                )
            ).all()
        )
        assert actual_event_evidence == expected_batch_evidence == {evidence.id}
        identifiers = {
            "org_id": organization.id,
            "event_id": event.id,
            "evidence_id": evidence.id,
            "foreign_evidence_id": foreign_evidence_id,
        }
        session.commit()

    _assert_commit_rejects(
        postgres_engine,
        authority,
        sa.text(
            "DELETE FROM event_evidence WHERE org_id = :org_id "
            "AND event_id = :event_id AND evidence_id = :evidence_id"
        ),
        identifiers,
    )
    _assert_commit_rejects(
        postgres_engine,
        authority,
        sa.text(
            "INSERT INTO event_evidence (org_id, event_id, evidence_id, relation_kind) "
            "VALUES (:org_id, :event_id, :foreign_evidence_id, 'supporting')"
        ),
        identifiers,
    )

    with Session(postgres_engine) as session:
        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reverse = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=identifiers["org_id"],
                    event_id=identifiers["event_id"],
                    idempotency_key="r4-event-evidence-reversal",
                    reason="R4 证据继承",
                    posting_date=date(2026, 3, 6),
                )
            )
        assert reverse.status == "posted", reverse.errors
        inherited = set(
            session.scalars(
                select(event_evidence.c.evidence_id).where(
                    event_evidence.c.org_id == identifiers["org_id"],
                    event_evidence.c.event_id == reverse.event_id,
                    event_evidence.c.relation_kind == "inherited",
                )
            ).all()
        )
        assert inherited == {identifiers["evidence_id"]}
        session.commit()


def _post_expense_event(
    session: Session, organization: object, authority, evidence_id, *, key: str
) -> BusinessEvent:
    with authority.attributed_call(session, tool_name="finance_record_event"):
        result = FinanceService(session).record_event(
            RecordEventRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": key,
                    "posting_date": "2026-03-05",
                    "evidence_references": [evidence_id],
                    "components": [
                        {
                            "key": "expense",
                            "kind": "expense",
                            "business_date": "2026-03-05",
                            "payment_date": "2026-03-05",
                            "amount_fen": 100,
                            "expense_class": "general_expense",
                            "payment_basis": "supplier_credit",
                            "counterparty": {
                                "kind": "supplier",
                                "name": "R4 测试供应商",
                            },
                        }
                    ],
                }
            )
        )
    assert result.status == "posted", result.errors
    event = session.get(BusinessEvent, result.event_id)
    assert event is not None
    return event


def _post_unmatched_expense_for_invariant(
    session: Session, organization: object, authority, evidence_id, *, key: str
) -> BusinessEvent:
    """Build a legal final expense without a bank edge to isolate voucher assertions."""
    return _post_expense_event(
        session,
        organization,
        authority,
        evidence_id,
        key=key,
    )


def test_r4_005_rejects_fake_payroll_reversal_of_sale_and_requires_exact_voucher(
    postgres_database,
) -> None:
    """A draft payroll_accrual cannot masquerade as a reversal of a normal expense."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        original = _post_expense_event(
            session,
            organization,
            authority,
            evidence_id,
            key="r4-original-expense",
        )
        original_voucher = session.scalar(select(Voucher).where(Voucher.event_id == original.id))
        assert original_voucher is not None
        original_component = session.scalar(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == original.id)
        )
        assert original_component is not None
        identifiers = {
            "org_id": organization.id,
            "original_event_id": original.id,
            "original_voucher_id": original_voucher.id,
            "original_component_id": original_component.id,
        }
        session.commit()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["original_event_id"])
        original_voucher = session.get(Voucher, identifiers["original_voucher_id"])
        assert original is not None and original_voucher is not None
        with pytest.raises(DBAPIError):
            with authority.attributed_call(
                session, tool_name="finance_test_fake_payroll_reversal"
            ) as attribution:
                fake = BusinessEvent(
                    org_id=identifiers["org_id"],
                    idempotency_key="r4-fake-payroll-reversal",
                    event_type="payroll_accrual",
                    status="draft",
                    description="伪造工资冲正",
                    facts={
                        "original_event_id": str(original.id),
                        "source_event_id": str(original.id),
                        "source_component_id": str(identifiers["original_component_id"]),
                        "reversal": True,
                    },
                    business_date=date(2026, 3, 6),
                    posting_date=date(2026, 3, 6),
                    rule_trace=[],
                    execution_attribution_id=attribution.id,
                )
                session.add(fake)
                session.flush()
                session.execute(
                    event_evidence.insert().from_select(
                        ["org_id", "event_id", "evidence_id", "relation_kind"],
                        select(
                            event_evidence.c.org_id,
                            sa.literal(fake.id),
                            event_evidence.c.evidence_id,
                            sa.literal("inherited"),
                        ).where(event_evidence.c.event_id == original.id),
                    )
                )
                original_lines = original_voucher.lines
                create_voucher(
                    session,
                    event=fake,
                    posting_date=date(2026, 3, 6),
                    description="故意省略原凭证关联",
                    entries=[
                        Entry(
                            account_code=line.account.code,
                            debit_fen=line.credit_fen,
                            credit_fen=line.debit_fen,
                            counterparty_id=line.counterparty_id,
                        )
                        for line in original_lines
                    ],
                )
                original.status = "reversed"
                original.reversed_by_event_id = fake.id
                fake.status = "posted"
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["original_event_id"])
        assert original is not None and original.status == "posted"
        assert (
            session.scalar(
                select(BusinessEvent.id).where(
                    BusinessEvent.org_id == identifiers["org_id"],
                    BusinessEvent.idempotency_key == "r4-fake-payroll-reversal",
                )
            )
            is None
        )


def test_r4_005_final_reversal_voucher_lines_are_immutable_and_link_is_checked(
    postgres_database,
) -> None:
    """A final reversal voucher freezes its lines and cannot lose the original-voucher link."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        original = _post_expense_event(
            session, organization, authority, evidence_id, key="r4-exact-original"
        )
        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            reversed_result = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=original.id,
                    idempotency_key="r4-exact-reversal",
                    reason="R4 精确凭证",
                    posting_date=date(2026, 3, 6),
                )
            )
        assert reversed_result.status == "posted", reversed_result.errors
        reversal_voucher = session.get(Voucher, reversed_result.voucher_id)
        assert reversal_voucher is not None
        identifiers = {
            "reversal_voucher_id": reversal_voucher.id,
            "original_voucher_id": reversal_voucher.reversal_of_voucher_id,
        }
        assert identifiers["original_voucher_id"] is not None
        session.commit()

    with Session(postgres_engine) as session:
        lines = session.scalars(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == identifiers["reversal_voucher_id"])
            .order_by(VoucherLine.line_number)
        ).all()
        assert len(lines) == 2
        with pytest.raises(DBAPIError, match="lines of a final voucher are immutable"):
            with authority.attributed_call(session, tool_name="finance_test_reversal_line_attack"):
                session.execute(
                    sa.text(
                        "UPDATE voucher_lines SET account_id = :account_id WHERE id = :line_id"
                    ),
                    {"account_id": lines[1].account_id, "line_id": lines[0].id},
                )
                session.execute(
                    sa.text(
                        "UPDATE voucher_lines SET account_id = :account_id WHERE id = :line_id"
                    ),
                    {"account_id": lines[0].account_id, "line_id": lines[1].id},
                )
                session.commit()
        session.rollback()

    _assert_commit_rejects(
        postgres_engine,
        authority,
        sa.text(
            "UPDATE vouchers SET reversal_of_voucher_id = NULL WHERE id = :reversal_voucher_id"
        ),
        identifiers,
    )


def test_r4_005_draft_reversal_with_balanced_noninverse_lines_fails_at_commit(
    postgres_database,
) -> None:
    """The exact-inverse assertion rejects a malformed voucher before it becomes final."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        original = _post_unmatched_expense_for_invariant(
            session,
            organization,
            authority,
            evidence_id,
            key="r4-noninverse-original",
        )
        original_voucher = session.scalar(select(Voucher).where(Voucher.event_id == original.id))
        assert original_voucher is not None
        original_component = session.scalar(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == original.id)
        )
        assert original_component is not None
        with pytest.raises(DBAPIError, match="reversal voucher lines must exactly reverse"):
            with authority.attributed_call(
                session, tool_name="finance_test_noninverse_reversal"
            ) as attribution:
                fake = BusinessEvent(
                    org_id=organization.id,
                    idempotency_key="r4-noninverse-reversal",
                    event_type="reversal",
                    status="draft",
                    description="金额平衡但非精确反向",
                    facts={
                        "original_event_id": str(original.id),
                        "source_event_id": str(original.id),
                        "source_component_id": str(original_component.id),
                        "reversal": True,
                    },
                    business_date=date(2026, 3, 6),
                    posting_date=date(2026, 3, 6),
                    rule_trace=[],
                    execution_attribution_id=attribution.id,
                )
                session.add(fake)
                session.flush()
                session.execute(
                    event_evidence.insert().from_select(
                        ["org_id", "event_id", "evidence_id", "relation_kind"],
                        select(
                            event_evidence.c.org_id,
                            sa.literal(fake.id),
                            event_evidence.c.evidence_id,
                            sa.literal("inherited"),
                        ).where(event_evidence.c.event_id == original.id),
                    )
                )
                create_voucher(
                    session,
                    event=fake,
                    posting_date=date(2026, 3, 6),
                    description="故意保留原始借贷方向",
                    reversal_of=original_voucher,
                    entries=[
                        Entry(
                            account_code=line.account.code,
                            debit_fen=line.debit_fen,
                            credit_fen=line.credit_fen,
                            counterparty_id=line.counterparty_id,
                        )
                        for line in original_voucher.lines
                    ],
                )
                original.status = "reversed"
                original.reversed_by_event_id = fake.id
                fake.status = "posted"
                session.commit()
        session.rollback()


def test_r4_006_statutory_edges_keep_each_direct_source_and_query_the_normalized_graph(
    postgres_database,
) -> None:
    """Each statutory settlement has an own normalized source edge visible to callers."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        organization = session.get(Organization, org_id)
        statutory, settled_items = _post_two_partial_salary_social_payment(
            session, organization, authority, evidence_id
        )
        edges = session.scalars(
            select(PayrollEventLink)
            .where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == statutory.event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
            .order_by(PayrollEventLink.source_open_item_id)
        ).all()
        assert len(edges) == len(settled_items) == 3
        assert {edge.source_open_item_id for edge in edges} == {item.id for item in settled_items}

        items_by_id = {item.id: item for item in settled_items}
        source_components_by_id = {
            component.id: component
            for component in session.scalars(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.id.in_(
                        {item.source_component_id for item in settled_items}
                    )
                )
            ).all()
        }
        assert all(edge.source_payment_event_id is not None for edge in edges)
        for edge in edges:
            source_item = items_by_id[edge.source_open_item_id]
            source_component = source_components_by_id[source_item.source_component_id]
            assert edge.payroll_batch_id is not None
            if source_item.payable_category == "employer_social":
                assert source_component.kind == "payroll_accrual"
            else:
                assert source_item.payable_category == "withheld_employee_social"
                assert source_component.kind == "salary_settlement"

        lifecycle = FinanceService(session).get_payroll_batch(
            organization.id, edges[0].payroll_batch_id
        )["lifecycle"]
        queried_edges = {item["id"]: item for item in lifecycle["payroll_event_links"]}
        assert {str(edge.id) for edge in edges} <= queried_edges.keys()
        for edge in edges:
            queried = queried_edges[str(edge.id)]
            assert queried["payroll_batch"]["policy_version_id"] is not None
            assert queried["source_payment_event"]["status"] == "posted"
            assert queried["source_open_item"]["source_event_id"] == str(
                edge.source_payment_event_id
            )
            assert (
                queried["source_open_item"]["payable_category"]
                == items_by_id[edge.source_open_item_id].payable_category
            )
            assert (
                queried["source_open_item"]["insurance_kind"]
                == items_by_id[edge.source_open_item_id].insurance_kind
            )
        session.commit()


def _salary_payment_with_unsettled_statutory_sources(
    session: Session,
    org_id,
    evidence_id,
    authority,
    *,
    key: str,
) -> dict[str, object]:
    """Create one genuine salary source and leave its statutory items open.

    The direct-SQL attacks below intentionally bypass the service.  This helper
    makes the *source* side canonical first, so their rejection proves the
    PostgreSQL PEL contract rather than a missing prerequisite of a forged
    event.
    """

    organization = session.get(Organization, org_id)
    prepare_authenticated_bank_account(
        session,
        organization,
        authority=authority,
        evidence_id=evidence_id,
    )
    organization, batch, line, _evidence_row, confirmed_event = confirmed_payroll(
        session,
        org_id,
        evidence_id,
        authority,
        key=f"r4-statutory-{key}",
    )
    salary_item = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == confirmed_event.id,
            OpenItem.payable_category == "salary",
        )
    )
    assert salary_item is not None and batch is not None and line is not None
    with authority.attributed_call(session, tool_name="finance_record_event"):
        salary_payment = FinanceService(session).record_event(
            payment_request(
                organization,
                event_type="salary_payment",
                amount_fen=425_000,
                allocations=[{"open_item_id": salary_item.id, "amount_fen": 500_000}],
                salary_withholdings=[
                    {
                        "open_item_id": salary_item.id,
                        "employee_social_insurance_items": {"pension": 40_000},
                        "employee_housing_fund_items": {"housing_fund": 35_000},
                        "individual_income_tax_fen": 0,
                    }
                ],
                bank=add_bank_row(session, organization, -425_000, f"r4-statutory-{key}-salary"),
                key=f"r4-statutory-{key}-salary",
            )
        )
    assert salary_payment.status == "posted", salary_payment.errors
    items = {
        item.payable_category: item
        for item in session.scalars(
            select(OpenItem).where(
                OpenItem.org_id == organization.id,
                OpenItem.source_event_id == salary_payment.event_id,
                OpenItem.payable_category.in_(
                    ("withheld_employee_social", "withheld_employee_housing")
                ),
            )
        ).all()
    }
    assert set(items) == {"withheld_employee_social", "withheld_employee_housing"}
    source_batch_id = session.scalar(
        select(PayrollEventLink.payroll_batch_id).where(
            PayrollEventLink.org_id == organization.id,
            PayrollEventLink.event_id == salary_payment.event_id,
            PayrollEventLink.link_kind == "salary_payment",
        )
    )
    assert source_batch_id == batch.id
    unrelated_batch, _unrelated_line, _unrelated_event = _draft_batch_and_event(
        session,
        authority,
        batch,
        line,
        key=f"{key}-unrelated",
        payroll_period=batch.payroll_period,
        version=batch.version + 1,
    )
    supplier_event = _post_expense_event(
        session,
        organization,
        authority,
        evidence_id,
        key=f"r4-statutory-{key}-supplier",
    )
    supplier_item = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == supplier_event.id,
            OpenItem.item_type == "payable",
            OpenItem.payable_category.is_(None),
        )
    )
    assert supplier_item is not None
    wrong_batch_bank = add_bank_row(
        session, organization, -40_000, f"r4-statutory-{key}-wrong-batch"
    )
    wrong_category_bank = add_bank_row(
        session,
        organization,
        -supplier_item.original_amount_fen,
        f"r4-statutory-{key}-wrong-category",
    )
    return {
        "org_id": organization.id,
        "source_batch_id": source_batch_id,
        "unrelated_batch_id": unrelated_batch.id,
        "withheld_social_item_id": items["withheld_employee_social"].id,
        "withheld_housing_item_id": items["withheld_employee_housing"].id,
        "supplier_item_id": supplier_item.id,
        "wrong_batch_bank_id": wrong_batch_bank.id,
        "wrong_category_bank_id": wrong_category_bank.id,
        "evidence_id": evidence_id,
    }


def _stage_raw_statutory_settlement(
    session: Session,
    *,
    org_id: object,
    source_item: OpenItem,
    payroll_batch_id: object,
    key: str,
    bank_transaction_id: object,
    evidence_id: object,
    execution_attribution_id: object,
) -> BusinessEvent:
    """Forge a fully-funded statutory event so only the PEL invariant can reject it."""

    payable_roles = {
        "withheld_employee_social": "withheld_employee_social_payable",
        "withheld_employee_housing": "withheld_employee_housing_fund_payable",
        "employer_social": "employer_social_payable",
        "employer_housing": "employer_housing_payable",
        "individual_income_tax": "individual_income_tax_payable",
        None: "accounts_payable",
    }
    payable_role = payable_roles[source_item.payable_category]
    amount_fen = source_item.original_amount_fen - source_item.settled_amount_fen
    assert amount_fen > 0
    event = BusinessEvent(
        org_id=org_id,
        idempotency_key=key,
        event_type="composite",
        status="draft",
        description="R4 法定缴款来源边直接 SQL 反例",
        facts={
            "amounts": {"amount_fen": amount_fen, "currency": "CNY"},
            "business_dates": {
                "business_date": "2026-03-06",
                "payment_date": "2026-03-06",
                "posting_date": "2026-03-06",
            },
            "bank_account_code": "1002",
        },
        business_date=date(2026, 3, 6),
        payment_date=date(2026, 3, 6),
        posting_date=date(2026, 3, 6),
        rule_trace=[],
        execution_attribution_id=execution_attribution_id,
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

    def forge_link(target_session, target_event, component):
        bank_transaction = target_session.get(BankTransaction, bank_transaction_id)
        assert bank_transaction is not None
        bank_transaction.matched_event_id = target_event.id
        target_session.add(
            BankTransactionMatch(
                org_id=org_id,
                bank_transaction_id=bank_transaction_id,
                event_id=target_event.id,
            )
        )
        target_session.add(
            PayrollEventLink(
                org_id=org_id,
                event_id=target_event.id,
                payroll_batch_id=payroll_batch_id,
                source_payment_event_id=source_item.source_event_id,
                source_open_item_id=source_item.id,
                link_kind="statutory_payment",
                component_id=component.id,
            )
        )

    commit_posting_plan(
        session,
        event=event,
        posting_date=date(2026, 3, 6),
        description=event.description,
        components=[
            ComponentPostingPlan(
                key="statutory",
                kind="payable_settlement",
                facts={
                    "amount_fen": amount_fen,
                    "payment_date": "2026-03-06",
                },
                entries=[
                    Entry(
                        account_role=payable_role,
                        debit_fen=amount_fen,
                        counterparty_id=source_item.counterparty_id,
                    ),
                    Entry(account_role="bank", credit_fen=amount_fen),
                ],
                settlements=[
                    SettlementPlan(
                        amount_fen=amount_fen,
                        purpose="payable_settlement",
                        expected_item_type="payable",
                        counterparty_id=source_item.counterparty_id,
                        open_item_id=source_item.id,
                    )
                ],
                cash_flows=[
                    CashFlowPlan(
                        bank_account_code="1002",
                        category="cash_flow_6",
                        amount_fen=-amount_fen,
                    )
                ],
                effects=[forge_link],
            )
        ],
    )
    return event


def test_r4_006_statutory_source_edge_rejects_wrong_payroll_batch_at_commit(
    postgres_database,
) -> None:
    """A genuine salary deduction cannot be claimed by an unrelated draft batch."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        identifiers = _salary_payment_with_unsettled_statutory_sources(
            session, org_id, evidence_id, authority, key="wrong-batch"
        )
        session.commit()

    with Session(postgres_engine) as session:
        source_item = session.get(OpenItem, identifiers["withheld_social_item_id"])
        assert source_item is not None
        with authority.attributed_call(
            session, tool_name="finance_test_statutory_wrong_batch"
        ) as attribution:
            _stage_raw_statutory_settlement(
                session,
                org_id=identifiers["org_id"],
                source_item=source_item,
                payroll_batch_id=identifiers["unrelated_batch_id"],
                key="r4-statutory-wrong-batch",
                bank_transaction_id=identifiers["wrong_batch_bank_id"],
                evidence_id=identifiers["evidence_id"],
                execution_attribution_id=attribution.id,
            )
            with pytest.raises(DBAPIError, match="PAYROLL_EVENT_LINK_BATCH_ORIGIN_INVALID"):
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        source_item = session.get(OpenItem, identifiers["withheld_social_item_id"])
        assert source_item is not None and source_item.status == "open"
        assert source_item.settled_amount_fen == 0


def test_r4_006_statutory_source_edge_rejects_wrong_payable_category_at_commit(
    postgres_database,
) -> None:
    """A supplier payable cannot be claimed by a statutory payroll source edge."""

    postgres_engine, org_id, evidence_id, authority = postgres_database
    with Session(postgres_engine) as session:
        identifiers = _salary_payment_with_unsettled_statutory_sources(
            session, org_id, evidence_id, authority, key="wrong-category"
        )
        session.commit()

    with Session(postgres_engine) as session:
        source_item = session.get(OpenItem, identifiers["supplier_item_id"])
        assert source_item is not None
        with authority.attributed_call(
            session, tool_name="finance_test_statutory_wrong_category"
        ) as attribution:
            _stage_raw_statutory_settlement(
                session,
                org_id=identifiers["org_id"],
                source_item=source_item,
                payroll_batch_id=identifiers["source_batch_id"],
                key="r4-statutory-wrong-category",
                bank_transaction_id=identifiers["wrong_category_bank_id"],
                evidence_id=identifiers["evidence_id"],
                execution_attribution_id=attribution.id,
            )
            with pytest.raises(DBAPIError, match="PAYROLL_EVENT_LINK_OPEN_ITEM_ORIGIN_INVALID"):
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        source_item = session.get(OpenItem, identifiers["supplier_item_id"])
        assert source_item is not None and source_item.status == "open"
        assert source_item.settled_amount_fen == 0
