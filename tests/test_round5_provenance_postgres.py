"""PostgreSQL commit-boundary regression coverage for R5 provenance rules.

These are deliberately not unit mocks: each attack starts from a canonical
service-created source, bypasses the service only for the malicious draft
transition, and proves that PostgreSQL rejects it at ``COMMIT``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from copy import deepcopy
from datetime import date, datetime

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from test_payroll_service import (
    add_bank_row,
    payment_request,
    payroll_parameters,
    register_payroll_facts,
)
from test_round3_lineage import _evidence
from test_round4_event_integrity_postgres import _salary_payment_with_unsettled_statutory_sources
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.ledger import Entry, create_voucher
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    OpenItem,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    Voucher,
    event_evidence,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    """Install the complete migration head in a clean PostgreSQL 17 instance."""

    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(database_url)
        try:
            yield engine
        finally:
            engine.dispose()


def _stage_exact_inverse_reversal(
    session: Session,
    original: BusinessEvent,
    *,
    key: str,
    event_type: str = "reversal",
) -> BusinessEvent:
    """Build an otherwise canonical draft reversal without calling the service."""

    original_voucher = session.scalar(select(Voucher).where(Voucher.event_id == original.id))
    assert original_voucher is not None
    reversal = BusinessEvent(
        org_id=original.org_id,
        idempotency_key=key,
        event_type=event_type,
        status="draft",
        description="R5 直接SQL冲正攻击",
        facts={"original_event_id": str(original.id), "reversal": True},
        business_date=date(2026, 3, 6),
        posting_date=date(2026, 3, 6),
        rule_trace=[],
        rule_version=original.rule_version,
    )
    session.add(reversal)
    session.flush()
    create_voucher(
        session,
        event=reversal,
        posting_date=date(2026, 3, 6),
        description=reversal.description,
        reversal_of=original_voucher,
        entries=[
            Entry(
                account_code=line.account.code,
                debit_fen=line.credit_fen,
                credit_fen=line.debit_fen,
                counterparty_id=line.counterparty_id,
            )
            for line in original_voucher.lines
        ],
    )
    return reversal


def _post_expense_with_supporting_evidence(
    session: Session, *, key: str
) -> tuple[object, BusinessEvent]:
    organization = seed_organization(
        session, accounting_period_control_enabled=False, name=f"R5 普通冲正证据 {key}"
    )
    evidence = _evidence(session, organization.id, f"r5-{key}-supporting")
    bank = add_bank_row(session, organization, -100, f"r5-{key}-bank")
    request = payment_request(
        organization,
        event_type="expense_cash",
        amount_fen=100,
        allocations=[],
        bank=bank,
        key=f"r5-{key}-expense",
    ).model_copy(update={"evidence_references": [evidence.id]})
    result = FinanceService(session).record_event(request)
    assert result.status == "posted", result.errors
    event = session.get(BusinessEvent, result.event_id)
    assert event is not None
    supporting = session.scalars(
        select(event_evidence.c.evidence_id).where(
            event_evidence.c.org_id == organization.id,
            event_evidence.c.event_id == event.id,
            event_evidence.c.relation_kind == "supporting",
        )
    ).all()
    assert supporting == [evidence.id]
    return organization, event


def test_r5_005_postgres_rejects_final_normal_reversal_without_inherited_evidence(
    postgres_engine: object,
) -> None:
    """A direct draft->posted normal reversal cannot drop the source evidence set."""

    with Session(postgres_engine) as session:
        organization, original = _post_expense_with_supporting_evidence(
            session, key="missing-inherited"
        )
        identifiers = {"org_id": organization.id, "original_event_id": original.id}
        session.commit()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["original_event_id"])
        assert original is not None
        reversal = _stage_exact_inverse_reversal(session, original, key="r5-missing-inherited")
        # This is the bypass: all voucher/state facts are otherwise canonical,
        # but the draft gets no ``inherited`` event_evidence edge at all.
        original.status = "reversed"
        original.reversed_by_event_id = reversal.id
        reversal.status = "posted"
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["original_event_id"])
        assert original is not None and original.status == "posted"
        assert original.reversed_by_event_id is None


def _stage_salary_reversal_then_delete_draft_pel(
    session: Session, *, org_id: object, original: BusinessEvent
) -> BusinessEvent:
    """Reproduce R5-005's deleted-in-draft PEL attack with every other effect intact."""

    reversal = _stage_exact_inverse_reversal(
        session, original, key="r5-delete-salary-reversal-link"
    )
    original_link = session.scalar(
        select(PayrollEventLink).where(
            PayrollEventLink.org_id == org_id,
            PayrollEventLink.event_id == original.id,
            PayrollEventLink.link_kind == "salary_payment",
        )
    )
    assert original_link is not None
    draft_link = PayrollEventLink(
        org_id=org_id,
        event_id=reversal.id,
        payroll_batch_id=original_link.payroll_batch_id,
        source_payment_event_id=original.id,
        source_open_item_id=original_link.source_open_item_id,
        link_kind="reversal",
    )
    session.add(draft_link)
    session.flush()
    # The edge exists while the event is draft, then an attacker removes it
    # before transition.  No final PEL immutability guard can catch this alone.
    session.delete(draft_link)

    for item in session.scalars(
        select(OpenItem).where(OpenItem.org_id == org_id, OpenItem.source_event_id == original.id)
    ):
        item.status = "reversed"
    settlements = session.scalars(
        select(Settlement)
        .where(
            Settlement.org_id == org_id,
            Settlement.payment_event_id == original.id,
            Settlement.reversed.is_(False),
        )
        .with_for_update()
    ).all()
    assert settlements
    for settlement in settlements:
        settlement.open_item.settled_amount_fen -= settlement.amount_fen
        settlement.open_item.status = (
            "open" if settlement.open_item.settled_amount_fen == 0 else "partial"
        )
        settlement.reversed = True
        # R5-001 makes this an organization-bound, auditable reversal relation.
        settlement.reversed_by_event_id = reversal.id

    matches = session.scalars(
        select(BankTransactionMatch)
        .where(
            BankTransactionMatch.org_id == org_id,
            BankTransactionMatch.event_id == original.id,
            BankTransactionMatch.invalidated_by_event_id.is_(None),
        )
        .with_for_update()
    ).all()
    for match in matches:
        match.invalidated_by_event_id = reversal.id
        match.invalidated_at = datetime.now().astimezone()
        bank = session.get(BankTransaction, match.bank_transaction_id)
        assert bank is not None
        bank.matched_event_id = None
    for allocation in session.scalars(
        select(PayrollWithholdingPaymentAllocation).where(
            PayrollWithholdingPaymentAllocation.org_id == org_id,
            PayrollWithholdingPaymentAllocation.payment_event_id == original.id,
            PayrollWithholdingPaymentAllocation.reversed.is_(False),
        )
    ):
        allocation.reversed = True
        allocation.reversed_by_event_id = reversal.id

    original.status = "reversed"
    original.reversed_by_event_id = reversal.id
    reversal.status = "posted"
    return reversal


def test_r5_005_postgres_rejects_salary_reversal_after_draft_pel_delete(
    postgres_engine: object,
) -> None:
    """A final salary-payment reversal must retain its exact canonical PEL."""

    with Session(postgres_engine) as session:
        identifiers = _salary_payment_with_unsettled_statutory_sources(
            session, key="delete-reversal-pel"
        )
        source_event_id = session.scalar(
            select(OpenItem.source_event_id).where(
                OpenItem.org_id == identifiers["org_id"],
                OpenItem.id == identifiers["withheld_social_item_id"],
            )
        )
        assert source_event_id is not None
        identifiers["salary_event_id"] = source_event_id
        session.commit()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["salary_event_id"])
        assert original is not None and original.event_type == "salary_payment"
        _stage_salary_reversal_then_delete_draft_pel(
            session, org_id=identifiers["org_id"], original=original
        )
        with pytest.raises(DBAPIError):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["salary_event_id"])
        assert original is not None and original.status == "posted"
        assert original.reversed_by_event_id is None


def test_r5_005_event_query_projects_relational_reversal_evidence_chain(
    postgres_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MCP event read model exposes supporting/inherited roles from relation tables."""

    with Session(postgres_engine) as session:
        organization, original = _post_expense_with_supporting_evidence(
            session, key="event-query-chain"
        )
        reversal = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=original.id,
                idempotency_key="r5-event-query-chain-reversal",
                reason="R5 查询规范冲正链",
                posting_date=date(2026, 3, 6),
            )
        )
        assert reversal.status == "posted", reversal.errors
        identifiers = {
            "org_id": organization.id,
            "original_event_id": original.id,
            "reversal_event_id": reversal.event_id,
        }
        session.commit()

    from ai_accounting import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        sessionmaker(bind=postgres_engine, expire_on_commit=False, autoflush=True),
    )
    response = mcp_server.finance_get_event(
        str(identifiers["org_id"]), str(identifiers["reversal_event_id"])
    )
    assert response["status"] == "ok"
    chain = response["canonical_reversal_chain"]
    assert chain["root_event_id"] == str(identifiers["original_event_id"])
    assert chain["terminal_event_id"] == str(identifiers["reversal_event_id"])
    by_event_id = {item["id"]: item for item in chain["events"]}
    assert by_event_id[str(identifiers["original_event_id"])]["reversed_by_event_id"] == str(
        identifiers["reversal_event_id"]
    )
    assert by_event_id[str(identifiers["reversal_event_id"])]["reversal_of_event_id"] == str(
        identifiers["original_event_id"]
    )
    evidence_roles = {(item["event_id"], item["relation_kind"]) for item in chain["event_evidence"]}
    assert (str(identifiers["original_event_id"]), "supporting") in evidence_roles
    assert (str(identifiers["reversal_event_id"]), "inherited") in evidence_roles


def _preview_regular(
    session: Session,
    *,
    org_id: object,
    employee_id: object,
    payroll_period: str,
    key: str,
) -> object:
    day = date.fromisoformat(f"{payroll_period}-05")
    result = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "batch_kind": "regular",
                "payroll_period": payroll_period,
                "posting_date": day.isoformat(),
                "payment_date": day.isoformat(),
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "base_salary_fen": 1_000_000,
                        "performance_pay_fen": 0,
                        "taxable_allowance_fen": 0,
                        "tax_exempt_income_fen": 0,
                        "attendance_deduction_fen": 0,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )
    assert not result.missing_information, result.missing_information
    assert result.status == "calculated", result.errors
    return result


def _preview_separate_bonus(
    session: Session,
    *,
    org_id: object,
    employee_id: object,
    key: str,
    payment_date: date = date(2026, 3, 5),
) -> object:
    result = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "batch_kind": "annual_bonus",
                "payroll_period": "2026-03",
                "posting_date": payment_date.isoformat(),
                "payment_date": payment_date.isoformat(),
                "tax_method": "separate",
                "employee_items": [{"employee_id": employee_id, "annual_bonus_fen": 100_000}],
            }
        )
    )
    assert result.status == "calculated", result.errors
    return result


def _confirm(session: Session, *, org_id: object, preview: object, key: str) -> object:
    result = FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=org_id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key=key,
        )
    )
    assert result.status == "posted", result.errors
    return result


def _post_full_salary_payment(
    session: Session,
    organization: object,
    *,
    batch_id: object,
    accrual_event_id: object,
    key: str,
) -> tuple[object, OpenItem]:
    """Pay one batch's salary and return its resulting individual-tax source item."""

    batch = session.get(PayrollBatch, batch_id)
    line = session.scalar(
        select(PayrollLine).where(
            PayrollLine.org_id == organization.id, PayrollLine.payroll_batch_id == batch_id
        )
    )
    salary = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == accrual_event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert batch is not None and line is not None and salary is not None
    bank = add_bank_row(session, organization, -line.net_salary_fen, f"{key}-bank")
    bank.booking_date = batch.payment_date
    request = payment_request(
        organization,
        event_type="salary_payment",
        amount_fen=line.net_salary_fen,
        allocations=[{"open_item_id": salary.id, "amount_fen": salary.original_amount_fen}],
        salary_withholdings=[
            {
                "open_item_id": salary.id,
                "employee_social_insurance_items": line.employee_social_insurance_items,
                "employee_housing_fund_items": line.employee_housing_fund_items,
                "individual_income_tax_fen": line.individual_income_tax_fen,
            }
        ],
        bank=bank,
        key=key,
    )
    request = request.model_copy(
        update={
            "business_dates": request.business_dates.model_copy(
                update={
                    "business_date": batch.payment_date,
                    "payment_date": batch.payment_date,
                    "posting_date": batch.posting_date,
                }
            )
        }
    )
    result = FinanceService(session).record_event(request)
    assert result.status == "posted", result.errors
    tax_item = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == result.event_id,
            OpenItem.payable_category == "individual_income_tax",
        )
    )
    assert tax_item is not None and tax_item.original_amount_fen > 0
    return result, tax_item


def _post_regular_tax_source(
    session: Session,
    organization: object,
    *,
    employee_id: object,
    payroll_period: str,
    key: str,
) -> tuple[object, OpenItem]:
    preview = _preview_regular(
        session,
        org_id=organization.id,
        employee_id=employee_id,
        payroll_period=payroll_period,
        key=f"{key}-preview",
    )
    confirmation = _confirm(
        session,
        org_id=organization.id,
        preview=preview,
        key=f"{key}-confirm",
    )
    _payment, tax_item = _post_full_salary_payment(
        session,
        organization,
        batch_id=preview.batch_id,
        accrual_event_id=confirmation.event_id,
        key=f"{key}-salary",
    )
    return preview, tax_item


def test_r5_007_compatible_multi_batch_tax_payment_keeps_per_source_provenance(
    postgres_engine: object,
) -> None:
    """Compatible regular and separate-bonus batches settle through one tax payment.

    The two formal batches share organization, tax agency, policy version,
    contribution/tax period and CNY, while their normalized PEL edges must
    retain their own batch, salary-payment event and open item.
    """

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 兼容多批次法定缴款"
        )
        employee_id = register_payroll_facts(session, organization)
        regular_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r5-multi-regular-preview",
        )
        regular = _confirm(
            session,
            org_id=organization.id,
            preview=regular_preview,
            key="r5-multi-regular-confirm",
        )
        bonus_preview = _preview_separate_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            key="r5-multi-bonus-preview",
        )
        bonus = _confirm(
            session,
            org_id=organization.id,
            preview=bonus_preview,
            key="r5-multi-bonus-confirm",
        )
        _regular_salary, regular_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=regular_preview.batch_id,
            accrual_event_id=regular.event_id,
            key="r5-multi-regular-salary",
        )
        _bonus_salary, bonus_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=bonus_preview.batch_id,
            accrual_event_id=bonus.event_id,
            key="r5-multi-bonus-salary",
        )
        amount_fen = regular_tax.original_amount_fen + bonus_tax.original_amount_fen
        request = payment_request(
            organization,
            event_type="individual_income_tax_payment",
            amount_fen=amount_fen,
            allocations=[
                {"open_item_id": regular_tax.id, "amount_fen": regular_tax.original_amount_fen},
                {"open_item_id": bonus_tax.id, "amount_fen": bonus_tax.original_amount_fen},
            ],
            bank=add_bank_row(session, organization, -amount_fen, "r5-multi-tax-bank"),
            key="r5-compatible-multi-batch-tax-payment",
        )
        tax_payment = FinanceService(session).record_event(request)
        assert tax_payment.status == "posted", tax_payment.errors
        replay = FinanceService(session).record_event(request)
        assert replay.status == "posted" and replay.event_id == tax_payment.event_id

        edges = session.scalars(
            select(PayrollEventLink)
            .where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == tax_payment.event_id,
                PayrollEventLink.link_kind == "statutory_payment",
            )
            .order_by(PayrollEventLink.source_open_item_id)
        ).all()
        assert len(edges) == 2
        assert {(edge.payroll_batch_id, edge.source_open_item_id) for edge in edges} == {
            (regular_preview.batch_id, regular_tax.id),
            (bonus_preview.batch_id, bonus_tax.id),
        }
        assert {edge.source_payment_event_id for edge in edges} == {
            regular_tax.source_event_id,
            bonus_tax.source_event_id,
        }

        lifecycle = FinanceService(session).get_payroll_batch(
            organization.id, regular_preview.batch_id
        )["lifecycle"]
        queried = {item["id"]: item for item in lifecycle["payroll_event_links"]}
        for edge in edges:
            relation = queried[str(edge.id)]
            assert relation["payroll_batch_id"] == str(edge.payroll_batch_id)
            assert relation["source_payment_event_id"] == str(edge.source_payment_event_id)
            assert relation["source_open_item_id"] == str(edge.source_open_item_id)
            assert relation["source_open_item"]["payable_category"] == "individual_income_tax"

        reversal = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=tax_payment.event_id,
                idempotency_key="r5-compatible-multi-batch-tax-reversal",
                reason="R5 多批次法定缴款冲正",
                posting_date=date(2026, 3, 6),
            )
        )
        assert reversal.status == "posted", reversal.errors
        reversal_edges = session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == reversal.event_id,
                PayrollEventLink.link_kind == "reversal",
            )
        ).all()
        assert {
            (edge.payroll_batch_id, edge.source_open_item_id, edge.source_payment_event_id)
            for edge in reversal_edges
        } == {
            (edge.payroll_batch_id, edge.source_open_item_id, tax_payment.event_id)
            for edge in edges
        }
        session.commit()


def test_r5_007_incompatible_tax_period_rejects_before_any_source_settlement(
    postgres_engine: object,
) -> None:
    """Known cross-period sources are rejected atomically, before partial settlement."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 法定缴款期间不兼容"
        )
        employee_id = register_payroll_facts(session, organization)
        september, september_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r5-period-september",
        )
        october, october_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-04",
            key="r5-period-october",
        )
        amount_fen = september_tax.original_amount_fen + october_tax.original_amount_fen
        bank = add_bank_row(session, organization, -amount_fen, "r5-period-incompatible-bank")
        bank.booking_date = date(2026, 4, 5)
        request = payment_request(
            organization,
            event_type="individual_income_tax_payment",
            amount_fen=amount_fen,
            allocations=[
                {"open_item_id": september_tax.id, "amount_fen": september_tax.original_amount_fen},
                {"open_item_id": october_tax.id, "amount_fen": october_tax.original_amount_fen},
            ],
            bank=bank,
            key="r5-period-incompatible-tax-payment",
        )
        request = request.model_copy(
            update={
                "business_dates": request.business_dates.model_copy(
                    update={
                        "business_date": date(2026, 4, 5),
                        "payment_date": date(2026, 4, 5),
                        "posting_date": date(2026, 4, 5),
                    }
                )
            }
        )
        result = FinanceService(session).record_event(request)
        assert result.status == "rejected"
        assert result.errors == ["STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES"]
        for item in (september_tax, october_tax):
            assert item.status == "open"
            assert item.settled_amount_fen == 0
        assert (
            session.scalar(
                select(Settlement.id).where(
                    Settlement.org_id == organization.id,
                    Settlement.open_item_id.in_([september_tax.id, october_tax.id]),
                    Settlement.reversed.is_(False),
                )
            )
            is None
        )
        assert september.batch_id != october.batch_id
        session.commit()


def test_r5_007_incompatible_policy_and_agency_reject_before_any_settlement(
    postgres_engine: object,
) -> None:
    """Two same-period sources with different policy/agency snapshots cannot merge."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 法定缴款政策机构不兼容"
        )
        service = FinanceService(session)
        employee = service.register_employee(
            RegisterEmployeeRequest(
                org_id=organization.id,
                employee_code="R5-POLICY-001",
                name="政策边界员工",
                employment_start_date=date(2026, 3, 1),
                status="active",
            )
        )
        employee_id = employee["employee_id"]
        assert employee["status"] == "registered"
        profile = service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=employee_id,
                effective_from=date(2026, 3, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=1_000_000,
                housing_fund_base_fen=1_000_000,
                resident_employee=True,
            )
        )
        assert profile["status"] == "registered"
        # A payroll period can have distinct tax-policy snapshots when payment
        # dates straddle a lawful effective-date boundary.  Contribution rules
        # still select the period-end policy, while the frozen batch policy ID
        # records the actual tax policy used on each payment date.
        first_parameters = deepcopy(payroll_parameters())
        first_policy = service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest(
                org_id=organization.id,
                region="R5 政策边界地区",
                effective_from=date(2025, 7, 1),
                effective_to=date(2026, 3, 5),
                version="r5-policy-agency-v1",
                source_url=(
                    "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/"
                    "201812/c4182700/content.html"
                ),
                parameters=first_parameters,
            )
        )
        assert first_policy["status"] == "registered"
        second_parameters = deepcopy(payroll_parameters())
        second_parameters["payment_targets"]["individual_income_tax"] = {
            "agency_code": "TAX-02",
            "agency_name": "第二税务局",
        }
        second_policy = service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest(
                org_id=organization.id,
                region="R5 政策边界地区",
                effective_from=date(2026, 3, 6),
                effective_to=date(2026, 6, 30),
                version="r5-policy-agency-v2",
                source_url=(
                    "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/"
                    "201812/c4182700/content.html"
                ),
                parameters=second_parameters,
            )
        )
        assert second_policy["status"] == "registered"

        regular_preview, regular_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r5-policy-agency-regular",
        )
        bonus_preview = _preview_separate_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            key="r5-policy-agency-bonus-preview",
            payment_date=date(2026, 3, 6),
        )
        bonus = _confirm(
            session,
            org_id=organization.id,
            preview=bonus_preview,
            key="r5-policy-agency-bonus-confirm",
        )
        _bonus_salary, bonus_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=bonus_preview.batch_id,
            accrual_event_id=bonus.event_id,
            key="r5-policy-agency-bonus-salary",
        )
        assert regular_preview.batch_id != bonus_preview.batch_id
        amount_fen = regular_tax.original_amount_fen + bonus_tax.original_amount_fen
        bank = add_bank_row(session, organization, -amount_fen, "r5-policy-agency-bank")
        bank.booking_date = date(2026, 3, 6)
        request = payment_request(
            organization,
            event_type="individual_income_tax_payment",
            amount_fen=amount_fen,
            allocations=[
                {"open_item_id": regular_tax.id, "amount_fen": regular_tax.original_amount_fen},
                {"open_item_id": bonus_tax.id, "amount_fen": bonus_tax.original_amount_fen},
            ],
            bank=bank,
            key="r5-policy-agency-incompatible-payment",
        )
        request = request.model_copy(
            update={
                "business_dates": request.business_dates.model_copy(
                    update={
                        "business_date": date(2026, 3, 6),
                        "payment_date": date(2026, 3, 6),
                        "posting_date": date(2026, 3, 6),
                    }
                )
            }
        )
        result = FinanceService(session).record_event(request)
        assert result.status == "rejected"
        assert result.errors == ["STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES"]
        for item in (regular_tax, bonus_tax):
            assert item.status == "open"
            assert item.settled_amount_fen == 0
        session.commit()


def test_r5_007_category_and_cross_organization_sources_reject_atomically(
    postgres_engine: object,
) -> None:
    """Category and organization are compatibility keys, never partial-payment hints."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 法定缴款类别隔离"
        )
        employee_id = register_payroll_facts(session, organization)
        preview, tax_item = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r5-category-local",
        )
        batch = session.get(PayrollBatch, preview.batch_id)
        assert batch is not None and batch.business_event_id is not None
        social_item = session.scalar(
            select(OpenItem).where(
                OpenItem.org_id == organization.id,
                OpenItem.source_event_id == batch.business_event_id,
                OpenItem.payable_category == "employer_social",
            )
        )
        assert social_item is not None

        category_amount = tax_item.original_amount_fen + social_item.original_amount_fen
        category_rejection = FinanceService(session).record_event(
            payment_request(
                organization,
                event_type="individual_income_tax_payment",
                amount_fen=category_amount,
                allocations=[
                    {"open_item_id": tax_item.id, "amount_fen": tax_item.original_amount_fen},
                    {
                        "open_item_id": social_item.id,
                        "amount_fen": social_item.original_amount_fen,
                    },
                ],
                bank=add_bank_row(
                    session, organization, -category_amount, "r5-category-rejection-bank"
                ),
                key="r5-category-rejection",
            )
        )
        assert category_rejection.status == "rejected"
        assert category_rejection.errors == ["STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES"]
        assert tax_item.status == social_item.status == "open"
        assert tax_item.settled_amount_fen == social_item.settled_amount_fen == 0

        foreign_organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 法定缴款跨企业"
        )
        foreign_employee_id = register_payroll_facts(session, foreign_organization)
        _foreign_preview, foreign_tax_item = _post_regular_tax_source(
            session,
            foreign_organization,
            employee_id=foreign_employee_id,
            payroll_period="2026-03",
            key="r5-category-foreign",
        )
        cross_org_amount = tax_item.original_amount_fen + foreign_tax_item.original_amount_fen
        cross_org_rejection = FinanceService(session).record_event(
            payment_request(
                organization,
                event_type="individual_income_tax_payment",
                amount_fen=cross_org_amount,
                allocations=[
                    {"open_item_id": tax_item.id, "amount_fen": tax_item.original_amount_fen},
                    {
                        "open_item_id": foreign_tax_item.id,
                        "amount_fen": foreign_tax_item.original_amount_fen,
                    },
                ],
                bank=add_bank_row(
                    session, organization, -cross_org_amount, "r5-cross-org-rejection-bank"
                ),
                key="r5-cross-org-rejection",
            )
        )
        assert cross_org_rejection.status == "rejected"
        assert cross_org_rejection.errors == ["STATUTORY_PAYMENT_SOURCE_OPEN_ITEM_NOT_FOUND"]
        for item in (tax_item, foreign_tax_item):
            assert item.status == "open"
            assert item.settled_amount_fen == 0
        session.commit()
