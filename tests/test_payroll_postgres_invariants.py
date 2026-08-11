from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import get_account_by_role, seed_organization
from ai_accounting.ledger import Entry, create_voucher
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventDependency,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    OpenItem,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollOpeningState,
    PayrollPolicyVersion,
    PayrollTaxStateSlot,
    PayrollWithholdingEntitlement,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    Voucher,
    VoucherLine,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
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


def _policy(session: Session, org_id: object, *, version: str = "2025.1") -> PayrollPolicyVersion:
    policy = PayrollPolicyVersion(
        org_id=org_id,
        region="CN-310000",
        effective_from=date(2025, 7, 1),
        version=version,
        source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        parameters={"social_insurance": {}, "housing_fund": {}},
    )
    session.add(policy)
    session.flush()
    return policy


def _employee_profile(
    session: Session, org_id: object, *, code: str = "E-001"
) -> tuple[Employee, EmployeePayrollProfileVersion]:
    counterparty = Counterparty(org_id=org_id, kind="employee", name=f"员工 {code}")
    session.add(counterparty)
    session.flush()
    employee = Employee(
        org_id=org_id,
        counterparty_id=counterparty.id,
        employee_code=code,
        name=f"员工 {code}",
        employment_start_date=date(2025, 7, 1),
    )
    session.add(employee)
    session.flush()
    profile = EmployeePayrollProfileVersion(
        org_id=org_id,
        employee_id=employee.id,
        effective_from=date(2025, 7, 1),
        expense_role="payroll_management_expense",
        social_insurance_base_fen=0,
        housing_fund_base_fen=0,
        resident_employee=True,
    )
    session.add(profile)
    session.flush()
    return employee, profile


def _line(
    *, org_id: object, batch_id: object, employee: Employee, profile: EmployeePayrollProfileVersion
) -> PayrollLine:
    return PayrollLine(
        org_id=org_id,
        payroll_batch_id=batch_id,
        employee_id=employee.id,
        employee_payroll_profile_version_id=profile.id,
        base_salary_fen=10_000,
        gross_salary_fen=10_000,
        net_salary_fen=10_000,
    )


def _event(
    session: Session, org_id: object, key: str, *, event_type: str = "expense_cash"
) -> BusinessEvent:
    event = BusinessEvent(
        org_id=org_id,
        idempotency_key=key,
        event_type=event_type,
        status="draft",
        description="工资计提",
        facts={},
        business_date=date(2026, 2, 28),
        posting_date=date(2026, 2, 28),
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    return event


def _make_final_batch(
    session: Session,
    org_id: object,
    policy: PayrollPolicyVersion,
    employee: Employee,
    profile: EmployeePayrollProfileVersion,
    *,
    key: str,
    version: int = 1,
    reversal_of: PayrollBatch | None = None,
    payroll_period: str = "2026-02",
) -> PayrollBatch:
    batch = PayrollBatch(
        org_id=org_id,
        idempotency_key=f"batch-{key}",
        batch_kind="regular",
        payroll_period=payroll_period,
        version=version,
        status="calculated",
        calculation_hash=(key * 64)[:64],
        request_payload_hash=("r" + key * 63)[:64],
        calculation_input={"employee_items": []},
        calculation_trace=[],
        policy_snapshot={"policy_version": policy.version},
        policy_version_id=policy.id,
        posting_date=date(2026, 2, 28),
        payment_date=date(2026, 3, 5),
        reversal_of_batch_id=reversal_of.id if reversal_of else None,
    )
    session.add(batch)
    session.flush()
    session.add(_line(org_id=org_id, batch_id=batch.id, employee=employee, profile=profile))
    if reversal_of is None:
        session.add(
            PayrollTaxStateSlot(
                org_id=org_id,
                employee_id=employee.id,
                tax_year=batch.payment_date.year,
                tax_month=batch.payment_date.month,
                regular_batch_id=batch.id,
                final_batch_id=batch.id,
            )
        )
    event = _event(session, org_id, f"event-{key}", event_type="payroll_accrual")
    original_event = (
        session.get(BusinessEvent, reversal_of.business_event_id)
        if reversal_of is not None and reversal_of.business_event_id is not None
        else None
    )
    original_voucher = (
        session.scalar(select(Voucher).where(Voucher.event_id == original_event.id))
        if original_event is not None
        else None
    )
    if original_event is not None:
        event.facts = {"original_event_id": str(original_event.id)}
    create_voucher(
        session,
        event=event,
        posting_date=date(2026, 2, 28),
        description="工资计提",
        entries=(
            [
                Entry(account_role="employee_salary_payable", debit_fen=10_000),
                Entry(account_role="payroll_management_expense", credit_fen=10_000),
            ]
            if original_event is not None
            else [
                Entry(account_role="payroll_management_expense", debit_fen=10_000),
                Entry(account_role="employee_salary_payable", credit_fen=10_000),
            ]
        ),
        reversal_of=original_voucher,
    )
    batch.business_event_id = event.id
    session.add(
        PayrollEventLink(
            org_id=org_id,
            event_id=event.id,
            payroll_batch_id=batch.id,
            source_payment_event_id=original_event.id if original_event is not None else None,
            link_kind="reversal" if original_event is not None else "payroll_accrual",
        )
    )
    session.flush()
    event.status = "posted"
    batch.status = "posted"
    if original_event is not None:
        original_event.status = "reversed"
        original_event.reversed_by_event_id = event.id
        reversal_of.status = "reversed"
    session.flush()
    return batch


def _make_final_withholding_batch(
    session: Session,
    org_id: object,
    policy: PayrollPolicyVersion,
    employee: Employee,
    profile: EmployeePayrollProfileVersion,
    *,
    key: str,
) -> tuple[PayrollBatch, PayrollLine, PayrollWithholdingEntitlement]:
    """Create a complete posted regular batch with one immutable pension fact."""

    batch = PayrollBatch(
        org_id=org_id,
        idempotency_key=f"withholding-{key}",
        batch_kind="regular",
        payroll_period="2026-02",
        version=1,
        status="calculated",
        calculation_hash=(key * 64)[:64],
        calculation_input={},
        calculation_trace=[],
        policy_snapshot={},
        policy_version_id=policy.id,
        posting_date=date(2026, 2, 28),
        payment_date=date(2026, 3, 5),
    )
    session.add(batch)
    session.flush()
    line = _line(org_id=org_id, batch_id=batch.id, employee=employee, profile=profile)
    line.employee_social_insurance_fen = 80
    line.employee_social_insurance_items = {"pension": 80}
    line.net_salary_fen = 9_920
    session.add(line)
    session.flush()
    entitlement = PayrollWithholdingEntitlement(
        org_id=org_id,
        payroll_line_id=line.id,
        contribution_group="employee_social_insurance",
        insurance_kind="pension",
        amount_fen=80,
    )
    session.add(entitlement)
    session.add(
        PayrollTaxStateSlot(
            org_id=org_id,
            employee_id=employee.id,
            tax_year=batch.payment_date.year,
            tax_month=batch.payment_date.month,
            regular_batch_id=batch.id,
            final_batch_id=batch.id,
        )
    )
    event = _event(session, org_id, f"withholding-event-{key}", event_type="payroll_accrual")
    create_voucher(
        session,
        event=event,
        posting_date=date(2026, 2, 28),
        description="工资计提",
        entries=[
            Entry(account_role="payroll_management_expense", debit_fen=10_000),
            Entry(account_role="employee_salary_payable", credit_fen=10_000),
        ],
    )
    batch.business_event_id = event.id
    session.add(
        PayrollEventLink(
            org_id=org_id,
            event_id=event.id,
            payroll_batch_id=batch.id,
            link_kind="payroll_accrual",
        )
    )
    session.flush()
    event.status = "posted"
    batch.status = "posted"
    session.flush()
    return batch, line, entitlement


def test_pay_014_final_payroll_batches_and_lines_are_immutable(postgres_engine: object) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="PAY-014 不变量"
        )
        policy = _policy(session, organization.id)
        employee, profile = _employee_profile(session, organization.id)
        batch = _make_final_batch(session, organization.id, policy, employee, profile, key="a")
        session.commit()
        batch_id = batch.id
        line_id = session.scalar(
            select(PayrollLine.id).where(PayrollLine.payroll_batch_id == batch_id)
        )

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        assert batch is not None
        batch.calculation_input = {"tampered": True}
        with pytest.raises(DBAPIError, match="posted payroll batches are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        line = session.get(PayrollLine, line_id)
        assert batch is not None and line is not None
        session.add(
            _line(
                org_id=batch.org_id,
                batch_id=batch.id,
                employee=session.get(Employee, line.employee_id),  # type: ignore[arg-type]
                profile=session.get(  # type: ignore[arg-type]
                    EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
                ),
            )
        )
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.get(PayrollLine, line_id)
        assert line is not None
        line.base_salary_fen = 10_001
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.get(PayrollLine, line_id)
        assert line is not None
        session.delete(line)
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        line = session.get(PayrollLine, line_id)
        assert batch is not None and line is not None
        draft = PayrollBatch(
            org_id=batch.org_id,
            idempotency_key="move-into-final",
            batch_kind="annual_bonus",
            payroll_period=batch.payroll_period,
            version=1,
            status="calculated",
            calculation_hash="m" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=batch.policy_version_id,
            posting_date=batch.posting_date,
            payment_date=batch.payment_date,
        )
        session.add(draft)
        session.flush()
        employee = session.get(Employee, line.employee_id)
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        assert employee is not None and profile is not None
        draft_line = _line(
            org_id=batch.org_id,
            batch_id=draft.id,
            employee=employee,
            profile=profile,
        )
        session.add(draft_line)
        session.flush()
        draft_line.payroll_batch_id = batch.id
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        line = session.get(PayrollLine, line_id)
        assert batch is not None and line is not None
        draft = PayrollBatch(
            org_id=batch.org_id,
            idempotency_key="move-out-of-final",
            batch_kind="annual_bonus",
            payroll_period=batch.payroll_period,
            version=1,
            status="calculated",
            calculation_hash="n" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=batch.policy_version_id,
            posting_date=batch.posting_date,
            payment_date=batch.payment_date,
        )
        session.add(draft)
        session.flush()
        line.payroll_batch_id = draft.id
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        original = session.get(PayrollBatch, batch_id)
        assert original is not None
        policy = session.get(PayrollPolicyVersion, original.policy_version_id)
        line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == original.id)
        )
        assert policy is not None and line is not None
        employee = session.get(Employee, line.employee_id)
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        assert employee is not None and profile is not None
        _make_final_batch(
            session,
            original.org_id,
            policy,
            employee,
            profile,
            key="b",
            version=2,
            reversal_of=original,
        )
        original.status = "reversed"
        session.commit()
        assert original.status == "reversed"


def test_pay_015_organization_links_and_final_shape_are_database_enforced(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session, accounting_period_control_enabled=False, name="PAY-015 企业 A"
        )
        organization_b = seed_organization(
            session, accounting_period_control_enabled=False, name="PAY-015 企业 B"
        )
        policy_a = _policy(session, organization_a.id, version="A")
        policy_b = _policy(session, organization_b.id, version="B")
        employee_a, profile_a = _employee_profile(session, organization_a.id, code="A")
        _, profile_b = _employee_profile(session, organization_b.id, code="B")
        organization_a_id = organization_a.id
        policy_a_id = policy_a.id
        policy_b_id = policy_b.id
        employee_a_id = employee_a.id
        profile_b_id = profile_b.id
        session.commit()

    with Session(postgres_engine) as session:
        batch = PayrollBatch(
            org_id=organization_a_id,
            idempotency_key="cross-policy",
            batch_kind="regular",
            payroll_period="2026-02",
            version=1,
            status="calculated",
            calculation_hash="c" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy_b_id,
            posting_date=date(2026, 2, 28),
            payment_date=date(2026, 3, 5),
        )
        session.add(batch)
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        batch = PayrollBatch(
            org_id=organization_a_id,
            idempotency_key="profile-parent",
            batch_kind="regular",
            payroll_period="2026-03",
            version=1,
            status="calculated",
            calculation_hash="d" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy_a_id,
            posting_date=date(2026, 3, 31),
            payment_date=date(2026, 4, 5),
        )
        session.add(batch)
        session.flush()
        session.add(
            _line(
                org_id=organization_a_id,
                batch_id=batch.id,
                employee=session.get(Employee, employee_a_id),  # type: ignore[arg-type]
                profile=session.get(EmployeePayrollProfileVersion, profile_b_id),  # type: ignore[arg-type]
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        incomplete = PayrollBatch(
            org_id=organization_a_id,
            idempotency_key="missing-event-and-lines",
            batch_kind="regular",
            payroll_period="2026-04",
            version=1,
            status="posted",
            calculation_hash="e" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy_a_id,
            posting_date=date(2026, 4, 30),
            payment_date=date(2026, 5, 5),
        )
        session.add(incomplete)
        with pytest.raises(DBAPIError, match="final payroll batch"):
            session.commit()


def test_pay_016_final_voucher_lines_reject_insert_update_and_delete(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="PAY-016 凭证不可变"
        )
        event = _event(session, organization.id, "pay-016-source")
        voucher = create_voucher(
            session,
            event=event,
            posting_date=date(2026, 2, 28),
            description="正式凭证",
            entries=[
                Entry(account_role="bank", debit_fen=100),
                Entry(account_role="service_revenue", credit_fen=100),
            ],
        )
        event.status = "posted"
        session.commit()
        voucher_id = voucher.id

    with Session(postgres_engine) as session:
        voucher = session.get(Voucher, voucher_id)
        assert voucher is not None
        bank = get_account_by_role(session, voucher.org_id, "bank")
        session.add(
            VoucherLine(
                org_id=voucher.org_id,
                voucher_id=voucher.id,
                line_number=3,
                account_id=bank.id,
                debit_fen=1,
            )
        )
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.scalar(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == voucher_id)
            .order_by(VoucherLine.line_number)
        )
        assert line is not None
        line.memo = "篡改"
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.scalar(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == voucher_id)
            .order_by(VoucherLine.line_number)
        )
        assert line is not None
        session.delete(line)
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()


def test_pay_017_open_item_settlement_conservation_and_org_links(postgres_engine: object) -> None:
    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session, accounting_period_control_enabled=False, name="PAY-017 企业 A"
        )
        organization_b = seed_organization(
            session, accounting_period_control_enabled=False, name="PAY-017 企业 B"
        )
        counterparty_a = Counterparty(org_id=organization_a.id, kind="supplier", name="机构 A")
        counterparty_b = Counterparty(org_id=organization_b.id, kind="supplier", name="机构 B")
        session.add_all([counterparty_a, counterparty_b])
        event_a = _event(session, organization_a.id, "pay-017-a")
        event_c = _event(session, organization_a.id, "pay-017-c")
        event_b = _event(session, organization_b.id, "pay-017-b")
        item = OpenItem(
            org_id=organization_a.id,
            counterparty_id=counterparty_a.id,
            source_event_id=event_a.id,
            item_type="payable",
            original_amount_fen=100,
            settled_amount_fen=0,
            status="open",
        )
        session.add(item)
        session.commit()
        item_id = item.id
        event_a_id = event_a.id
        event_c_id = event_c.id
        event_b_id = event_b.id

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        item.settled_amount_fen = 20
        item.status = "partial"
        with pytest.raises(DBAPIError, match="settlement total"):
            session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        item.settled_amount_fen = 40
        item.status = "partial"
        session.add(
            Settlement(
                org_id=item.org_id,
                open_item_id=item.id,
                payment_event_id=event_c_id,
                amount_fen=40,
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        item.settled_amount_fen = 100
        item.status = "settled"
        session.add(
            Settlement(
                org_id=item.org_id,
                open_item_id=item.id,
                payment_event_id=event_a_id,
                amount_fen=60,
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        settlement = session.scalar(
            select(Settlement)
            .where(Settlement.open_item_id == item_id)
            .order_by(Settlement.amount_fen)
        )
        assert item is not None and settlement is not None
        settlement.reversed = True
        item.settled_amount_fen = 60
        item.status = "partial"
        with pytest.raises(DBAPIError, match="ck_settlement_reversal_audit"):
            session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        session.add(
            Settlement(
                org_id=item.org_id,
                open_item_id=item.id,
                payment_event_id=event_b_id,
                amount_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_pay_018_postgresql_migration_round_trips_leave_no_payroll_objects() -> None:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            payroll_tables = {
                "employees",
                "employee_payroll_profile_versions",
                "payroll_policy_versions",
                "payroll_batches",
                "payroll_batch_version_sequences",
                "payroll_lines",
                "payroll_withholding_allocations",
                "payroll_withholding_entitlements",
                "payroll_withholding_payment_allocations",
                "payroll_opening_states",
                "annual_bonus_usages",
                "payroll_tax_state_slots",
                "payroll_event_links",
                "payroll_batch_evidence",
                "payroll_account_migration_actions",
            }
            assert payroll_tables <= set(inspect(engine).get_table_names())

            command.downgrade(config, "0001_initial")
            inspector = inspect(engine)
            assert payroll_tables.isdisjoint(inspector.get_table_names())
            assert "payable_category" not in {
                column["name"] for column in inspector.get_columns("open_items")
            }

            command.upgrade(config, "head")
            command.check(config)
            assert payroll_tables <= set(inspect(engine).get_table_names())

            command.downgrade(config, "base")
            assert payroll_tables.isdisjoint(inspect(engine).get_table_names())
        finally:
            engine.dispose()


def test_r3_011_open_item_state_preflight_rejects_pollution_without_partial_upgrade() -> None:
    """0003 data with a full balance labelled open must remain safely on 0003."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "0003_payroll_round2_integrity")
        engine = create_engine(url)
        try:
            organization_id = uuid.uuid4()
            counterparty_id = uuid.uuid4()
            event_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO organizations "
                        "(id, name, taxpayer_type, filing_cycle, jurisdiction, "
                        "urban_maintenance_rate, accounting_standard, created_at) VALUES "
                        "(:id, 'R3-011', 'small_scale', 'quarterly', 'CN', 0.07, "
                        "'small_enterprise', now())"
                    ),
                    {"id": organization_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO counterparties (id, org_id, kind, name) "
                        "VALUES (:id, :org_id, 'supplier', 'R3-011 supplier')"
                    ),
                    {"id": counterparty_id, "org_id": organization_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO business_events "
                        "(id, org_id, idempotency_key, event_type, status, description, facts, "
                        "business_date, posting_date, rule_trace, created_at) VALUES "
                        "(:id, :org_id, 'r3-011-source', 'payroll_accrual', 'rejected', "
                        "'迁移预检来源', '{}'::jsonb, '2026-02-28', '2026-02-28', "
                        "'[]'::jsonb, now())"
                    ),
                    {"id": event_id, "org_id": organization_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO open_items "
                        "(id, org_id, counterparty_id, source_event_id, item_type, "
                        "original_amount_fen, settled_amount_fen, status) VALUES "
                        "(:id, :org_id, :counterparty_id, :event_id, 'payable', 100, 0, 'open')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org_id": organization_id,
                        "counterparty_id": counterparty_id,
                        "event_id": event_id,
                    },
                )

            # 0003 already protects writes.  Simulate a legacy/operator
            # pollution that exists before 0004 is applied, without touching
            # its protected revision files or relying on service code.
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "ALTER TABLE open_items DISABLE TRIGGER "
                        "open_item_settlement_invariant_deferred"
                    )
                )
                connection.execute(
                    sa.text("UPDATE open_items SET settled_amount_fen = 100, status = 'open'")
                )
                connection.execute(
                    sa.text(
                        "ALTER TABLE open_items ENABLE TRIGGER "
                        "open_item_settlement_invariant_deferred"
                    )
                )

            with pytest.raises(RuntimeError, match="OPEN_ITEM_STATE_INVARIANT_VIOLATION"):
                command.upgrade(config, "head")

            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0003_payroll_round2_integrity"
                )
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM open_items")) == 1
        finally:
            engine.dispose()


def test_r4_004_event_evidence_cross_organization_preflight_keeps_0004() -> None:
    """0005 must not invent an organization for a polluted legacy evidence edge."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "0004_payroll_round3_integrity")
        engine = create_engine(url)
        try:
            first_org_id = uuid.uuid4()
            second_org_id = uuid.uuid4()
            event_id = uuid.uuid4()
            evidence_id = uuid.uuid4()
            migration_date = date(2026, 2, 10)
            with engine.begin() as connection:
                for organization_id, name in (
                    (first_org_id, "R4-004 first organization"),
                    (second_org_id, "R4-004 second organization"),
                ):
                    connection.execute(
                        sa.text(
                            "INSERT INTO organizations "
                            "(id, name, taxpayer_type, filing_cycle, jurisdiction, "
                            "urban_maintenance_rate, accounting_standard, created_at) VALUES "
                            "(:id, :name, 'small_scale', 'quarterly', 'CN', 0.07, "
                            "'small_enterprise', now())"
                        ),
                        {"id": organization_id, "name": name},
                    )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO business_events (
                            id, org_id, idempotency_key, event_type, status, description,
                            facts, business_date, posting_date, rule_trace, created_at
                        ) VALUES (
                            :id, :org_id, 'r4-evidence-preflight-event', 'expense_cash',
                            'rejected', '', '{}', :business_date, :posting_date, '[]', now()
                        )
                        """
                    ),
                    {
                        "id": event_id,
                        "org_id": first_org_id,
                        "business_date": migration_date,
                        "posting_date": migration_date,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO evidence (
                            id, org_id, sha256, original_name, media_type, source,
                            size_bytes, storage_path, metadata, created_at
                        ) VALUES (
                            :id, :org_id, :sha256, 'r4-cross-org.txt', 'text/plain',
                            'round4-migration-test', 1, '/r4/cross-org.txt', '{}', now()
                        )
                        """
                    ),
                    {"id": evidence_id, "org_id": second_org_id, "sha256": "4" * 64},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO event_evidence (event_id, evidence_id) "
                        "VALUES (:event_id, :evidence_id)"
                    ),
                    {"event_id": event_id, "evidence_id": evidence_id},
                )

            with pytest.raises(
                RuntimeError, match="EVENT_EVIDENCE_ORGANIZATION_INVARIANT_VIOLATION"
            ):
                command.upgrade(config, "head")

            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0004_payroll_round3_integrity"
                )
                assert "org_id" not in {
                    column["name"] for column in inspect(engine).get_columns("event_evidence")
                }
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM event_evidence")) == 1
        finally:
            engine.dispose()


def test_r4_011_0004_salary_source_backfill_is_proved_and_downgrade_is_safe() -> None:
    """Only the single real salary settlement can repair a polluted R4 source edge."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "0004_payroll_round3_integrity")
        engine = create_engine(url)
        try:
            organization_id = uuid.uuid4()
            counterparty_id = uuid.uuid4()
            accrual_event_id = uuid.uuid4()
            salary_payment_id = uuid.uuid4()
            salary_item_id = uuid.uuid4()
            policy_id = uuid.uuid4()
            batch_id = uuid.uuid4()
            salary_link_id = uuid.uuid4()
            salary_voucher_id = uuid.uuid4()
            salary_payable_account_id = uuid.uuid4()
            bank_account_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
                connection.execute(
                    sa.text(
                        "INSERT INTO organizations "
                        "(id, name, taxpayer_type, filing_cycle, jurisdiction, "
                        "urban_maintenance_rate, accounting_standard, created_at) VALUES "
                        "(:id, 'R4-011 source backfill', 'small_scale', 'quarterly', 'CN', "
                        "0.07, 'small_enterprise', now())"
                    ),
                    {"id": organization_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO counterparties (id, org_id, kind, name) VALUES "
                        "(:id, :org_id, 'employee', 'R4-011 employee')"
                    ),
                    {"id": counterparty_id, "org_id": organization_id},
                )
                for event_id, key, event_type, status in (
                    (accrual_event_id, "r4-011-accrual", "payroll_accrual", "draft"),
                    (salary_payment_id, "r4-011-salary-payment", "salary_payment", "posted"),
                ):
                    connection.execute(
                        sa.text(
                            "INSERT INTO business_events "
                            "(id, org_id, idempotency_key, event_type, status, description, facts, "
                            "business_date, posting_date, rule_trace, created_at) VALUES "
                            "(:id, :org_id, :key, :event_type, :status, '', '{}'::jsonb, "
                            "'2026-03-05', '2026-03-05', '[]'::jsonb, now())"
                        ),
                        {
                            "id": event_id,
                            "org_id": organization_id,
                            "key": key,
                            "event_type": event_type,
                            "status": status,
                        },
                    )
                for account_id, code, name, category, normal_side, role in (
                    (
                        salary_payable_account_id,
                        "2211.01",
                        "应付职工薪酬",
                        "liability",
                        "credit",
                        "employee_salary_payable",
                    ),
                    (bank_account_id, "1002", "银行存款", "asset", "debit", "bank"),
                ):
                    connection.execute(
                        sa.text(
                            "INSERT INTO accounts "
                            "(id, org_id, code, name, category, normal_side, system_role, active) "
                            "VALUES (:id, :org_id, :code, :name, :category, :normal_side, "
                            ":role, TRUE)"
                        ),
                        {
                            "id": account_id,
                            "org_id": organization_id,
                            "code": code,
                            "name": name,
                            "category": category,
                            "normal_side": normal_side,
                            "role": role,
                        },
                    )
                connection.execute(
                    sa.text(
                        "INSERT INTO vouchers "
                        "(id, org_id, event_id, voucher_number, posting_date, description, "
                        "status, posted_at) VALUES "
                        "(:id, :org_id, :event_id, 'R4-011-001', '2026-03-05', "
                        "'0004 历史工资付款', 'posted', now())"
                    ),
                    {
                        "id": salary_voucher_id,
                        "org_id": organization_id,
                        "event_id": salary_payment_id,
                    },
                )
                for line_number, account_id, debit_fen, credit_fen in (
                    (1, salary_payable_account_id, 1_000_000, 0),
                    (2, bank_account_id, 0, 1_000_000),
                ):
                    connection.execute(
                        sa.text(
                            "INSERT INTO voucher_lines "
                            "(id, voucher_id, org_id, line_number, account_id, debit_fen, "
                            "credit_fen, memo) VALUES "
                            "(:id, :voucher_id, :org_id, :line_number, :account_id, "
                            ":debit_fen, :credit_fen, '')"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "voucher_id": salary_voucher_id,
                            "org_id": organization_id,
                            "line_number": line_number,
                            "account_id": account_id,
                            "debit_fen": debit_fen,
                            "credit_fen": credit_fen,
                        },
                    )
                connection.execute(
                    sa.text(
                        "INSERT INTO open_items "
                        "(id, org_id, counterparty_id, source_event_id, item_type, "
                        "original_amount_fen, settled_amount_fen, status, payable_category) VALUES "
                        "(:id, :org_id, :counterparty_id, :source_event_id, 'payable', "
                        "1000000, 1000000, 'settled', 'salary')"
                    ),
                    {
                        "id": salary_item_id,
                        "org_id": organization_id,
                        "counterparty_id": counterparty_id,
                        "source_event_id": accrual_event_id,
                    },
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO payroll_policy_versions "
                        "(id, org_id, region, effective_from, effective_to, version, source_url, "
                        "parameters, created_at) VALUES "
                        "(:id, :org_id, 'CN', '2026-01-01', '2026-12-31', 'legacy', "
                        "'https://www.chinatax.gov.cn/', '{}'::jsonb, now())"
                    ),
                    {"id": policy_id, "org_id": organization_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO payroll_batches "
                        "(id, org_id, idempotency_key, batch_kind, payroll_period, version, "
                        "status, calculation_hash, calculation_input, calculation_trace, "
                        "policy_snapshot, policy_version_id, posting_date, payment_date, "
                        "business_event_id, created_at) VALUES "
                        "(:id, :org_id, 'r4-011-batch', 'regular', '2026-03', 1, "
                        "'calculated', :hash, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, "
                        ":policy_id, '2026-03-05', '2026-03-05', :event_id, now())"
                    ),
                    {
                        "id": batch_id,
                        "org_id": organization_id,
                        "hash": "4" * 64,
                        "policy_id": policy_id,
                        "event_id": accrual_event_id,
                    },
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO payroll_event_links "
                        "(id, org_id, event_id, payroll_batch_id, source_payment_event_id, "
                        "source_open_item_id, link_kind, created_at) VALUES "
                        "(:id, :org_id, :event_id, :batch_id, NULL, NULL, "
                        "'payroll_accrual', now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org_id": organization_id,
                        "event_id": accrual_event_id,
                        "batch_id": batch_id,
                    },
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO settlements "
                        "(id, org_id, open_item_id, payment_event_id, amount_fen, reversed) VALUES "
                        "(:id, :org_id, :open_item_id, :event_id, 1000000, FALSE)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org_id": organization_id,
                        "open_item_id": salary_item_id,
                        "event_id": salary_payment_id,
                    },
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO payroll_event_links "
                        "(id, org_id, event_id, payroll_batch_id, source_payment_event_id, "
                        "source_open_item_id, link_kind, created_at) VALUES "
                        "(:id, :org_id, :event_id, :batch_id, NULL, :open_item_id, "
                        "'salary_payment', now())"
                    ),
                    {
                        "id": salary_link_id,
                        "org_id": organization_id,
                        "event_id": salary_payment_id,
                        "batch_id": batch_id,
                        "open_item_id": salary_item_id,
                    },
                )

            # Model the only migration input that can be safely repaired: a
            # pre-R4 final salary edge whose one active settlement still proves
            # its source.  The legacy constraints are disabled solely to place
            # this historical pollution before 0005 begins.
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "ALTER TABLE payroll_event_links DISABLE TRIGGER "
                        "immutable_final_payroll_event_link"
                    )
                )
                connection.execute(
                    sa.text(
                        "ALTER TABLE payroll_event_links DISABLE TRIGGER "
                        "payroll_event_link_shape_deferred"
                    )
                )
                connection.execute(
                    sa.text(
                        "UPDATE payroll_event_links SET source_open_item_id = NULL WHERE id = :id"
                    ),
                    {"id": salary_link_id},
                )
                connection.execute(
                    sa.text(
                        "ALTER TABLE payroll_event_links ENABLE TRIGGER "
                        "immutable_final_payroll_event_link"
                    )
                )
                connection.execute(
                    sa.text(
                        "ALTER TABLE payroll_event_links ENABLE TRIGGER "
                        "payroll_event_link_shape_deferred"
                    )
                )

            command.upgrade(config, "head")
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text(
                            "SELECT source_open_item_id FROM payroll_event_links WHERE id = :id"
                        ),
                        {"id": salary_link_id},
                    )
                    == salary_item_id
                )

            with pytest.raises(
                RuntimeError, match="PAYROLL_DOWNGRADE_UNSAFE: source open-item lineage exists"
            ):
                command.downgrade(config, "0004_payroll_round3_integrity")
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0012_accounting_period_close"
                )
        finally:
            engine.dispose()


def test_r2_003_per_insurance_withholding_cannot_be_reallocated(
    postgres_engine: object,
) -> None:
    """A direct PostgreSQL write cannot spend the pension entitlement as medical."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R2-003"
        )
        policy = _policy(session, organization.id, version="r2-003")
        employee, profile = _employee_profile(session, organization.id, code="R2-003")
        batch = PayrollBatch(
            org_id=organization.id,
            idempotency_key="r2-003-batch",
            batch_kind="regular",
            payroll_period="2026-02",
            version=1,
            status="calculated",
            calculation_hash="3" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy.id,
            posting_date=date(2026, 2, 28),
            payment_date=date(2026, 3, 5),
        )
        session.add(batch)
        session.flush()
        line = _line(org_id=organization.id, batch_id=batch.id, employee=employee, profile=profile)
        session.add(line)
        session.flush()
        pension = PayrollWithholdingEntitlement(
            org_id=organization.id,
            payroll_line_id=line.id,
            contribution_group="employee_social_insurance",
            insurance_kind="pension",
            amount_fen=80,
        )
        medical = PayrollWithholdingEntitlement(
            org_id=organization.id,
            payroll_line_id=line.id,
            contribution_group="employee_social_insurance",
            insurance_kind="medical",
            amount_fen=20,
        )
        first_payment = _event(session, organization.id, "r2-003-payment-1")
        session.add_all([pension, medical])
        session.flush()
        session.add(
            PayrollWithholdingPaymentAllocation(
                org_id=organization.id,
                entitlement_id=pension.id,
                payment_event_id=first_payment.id,
                amount_fen=80,
            )
        )
        with pytest.raises(DBAPIError, match="final non-reversal payroll line"):
            session.commit()


def test_r3_003_posted_withholding_entitlements_and_allocations_are_append_only(
    postgres_engine: object,
) -> None:
    """Direct SQL cannot forge a zero-line entitlement or edit an active allocation."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-003"
        )
        policy = _policy(session, organization.id, version="r3-003")
        employee, profile = _employee_profile(session, organization.id, code="R3-003")
        _batch, _line, entitlement = _make_final_withholding_batch(
            session, organization.id, policy, employee, profile, key="r3w"
        )
        session.commit()
        organization_id = organization.id
        entitlement_id = entitlement.id

    with Session(postgres_engine) as session:
        entitlement = session.get(PayrollWithholdingEntitlement, entitlement_id)
        assert entitlement is not None
        entitlement.amount_fen = 81
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        entitlement = session.get(PayrollWithholdingEntitlement, entitlement_id)
        assert entitlement is not None
        session.delete(entitlement)
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        entitlement = session.get(PayrollWithholdingEntitlement, entitlement_id)
        assert entitlement is not None
        forged = PayrollWithholdingEntitlement(
            org_id=organization_id,
            payroll_line_id=entitlement.payroll_line_id,
            contribution_group="employee_social_insurance",
            insurance_kind="medical",
            amount_fen=100,
        )
        session.add(forged)
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        payment = _event(session, organization_id, "r3-003-salary")
        payment.event_type = "salary_payment"
        create_voucher(
            session,
            event=payment,
            posting_date=date(2026, 3, 5),
            description="工资支付",
            entries=[
                Entry(account_role="employee_salary_payable", debit_fen=80),
                Entry(account_role="bank", credit_fen=80),
            ],
        )
        payment.status = "posted"
        allocation = PayrollWithholdingPaymentAllocation(
            org_id=organization_id,
            entitlement_id=entitlement_id,
            payment_event_id=payment.id,
            amount_fen=80,
        )
        session.add(allocation)
        session.commit()
        allocation_id = allocation.id

    with Session(postgres_engine) as session:
        allocation = session.get(PayrollWithholdingPaymentAllocation, allocation_id)
        assert allocation is not None
        allocation.amount_fen = 81
        with pytest.raises(DBAPIError, match="immutable except formal reversal"):
            session.flush()

    with Session(postgres_engine) as session:
        allocation = session.get(PayrollWithholdingPaymentAllocation, allocation_id)
        assert allocation is not None
        allocation.reversed = True
        with pytest.raises(DBAPIError, match="immutable except formal reversal"):
            session.flush()


def test_r3_004_final_event_state_requires_draft_and_keeps_refund_original_posted(
    postgres_engine: object,
) -> None:
    """A direct reversed insert fails; refund reversal never rewrites the advance state."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-004"
        )
        direct = BusinessEvent(
            org_id=organization.id,
            idempotency_key="r3-004-direct-reversed",
            event_type="reversal",
            status="reversed",
            description="非法",
            facts={},
            business_date=date(2026, 3, 1),
            posting_date=date(2026, 3, 1),
            rule_trace=[],
        )
        session.add(direct)
        with pytest.raises(DBAPIError, match="created as draft"):
            session.flush()

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-004-refund"
        )
        advance = _event(session, organization.id, "r3-004-advance")
        advance.event_type = "customer_advance"
        advance.facts = {"amounts": {"gross_amount_fen": 100}}
        create_voucher(
            session,
            event=advance,
            posting_date=date(2026, 3, 1),
            description="客户预收",
            entries=[
                Entry(account_role="bank", debit_fen=100),
                Entry(account_role="contract_liability", credit_fen=100),
            ],
        )
        advance.status = "posted"
        refund = _event(session, organization.id, "r3-004-refund-event")
        refund.event_type = "customer_refund"
        refund.facts = {
            "amounts": {"amount_fen": 100},
            "details": {"original_event_id": str(advance.id), "refund_kind": "advance"},
        }
        refund_voucher = create_voucher(
            session,
            event=refund,
            posting_date=date(2026, 3, 2),
            description="客户退款",
            entries=[
                Entry(account_role="contract_liability", debit_fen=100),
                Entry(account_role="bank", credit_fen=100),
            ],
        )
        session.add(
            BusinessEventDependency(
                org_id=organization.id,
                parent_event_id=advance.id,
                child_event_id=refund.id,
                dependency_kind="advance_refund",
                amount_fen=100,
            )
        )
        session.flush()
        refund.status = "posted"
        refund_reversal = _event(session, organization.id, "r3-004-refund-reversal")
        refund_reversal.event_type = "reversal"
        refund_reversal.facts = {"original_event_id": str(refund.id)}
        create_voucher(
            session,
            event=refund_reversal,
            posting_date=date(2026, 3, 3),
            description="冲正客户退款",
            entries=[
                Entry(account_role="bank", debit_fen=100),
                Entry(account_role="contract_liability", credit_fen=100),
            ],
            reversal_of=refund_voucher,
        )
        refund_reversal.status = "posted"
        refund.status = "reversed"
        refund.reversed_by_event_id = refund_reversal.id
        session.commit()
        assert advance.status == "posted"
        assert refund.status == "reversed"


def test_r3_002_tax_state_slot_rejects_cross_employee_and_arbitrary_mutation(
    postgres_engine: object,
) -> None:
    """A slot is bound to the posted regular line's employee and payment month."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R3-002"
        )
        policy = _policy(session, organization.id, version="r3-002")
        employee_a, profile_a = _employee_profile(session, organization.id, code="R3-002-A")
        employee_b, profile_b = _employee_profile(session, organization.id, code="R3-002-B")
        regular_a = _make_final_batch(
            session, organization.id, policy, employee_a, profile_a, key="r3-002-a"
        )
        regular_b = _make_final_batch(
            session,
            organization.id,
            policy,
            employee_b,
            profile_b,
            key="r3-002-b",
            version=2,
            payroll_period="2026-03",
        )
        session.commit()
        organization_id = organization.id
        employee_a_id = employee_a.id
        employee_b_id = employee_b.id
        regular_a_id = regular_a.id
        regular_b_id = regular_b.id

    with Session(postgres_engine) as session:
        session.add(
            PayrollTaxStateSlot(
                org_id=organization_id,
                employee_id=employee_b_id,
                tax_year=2026,
                tax_month=8,
                regular_batch_id=regular_a_id,
                final_batch_id=regular_a_id,
            )
        )
        with pytest.raises(DBAPIError, match="same-employee regular payroll"):
            session.commit()

    with Session(postgres_engine) as session:
        slot_id = session.scalar(
            select(PayrollTaxStateSlot.id).where(
                PayrollTaxStateSlot.org_id == organization_id,
                PayrollTaxStateSlot.employee_id == employee_a_id,
                PayrollTaxStateSlot.regular_batch_id == regular_a_id,
            )
        )
        assert slot_id is not None

    with Session(postgres_engine) as session:
        slot = session.get(PayrollTaxStateSlot, slot_id)
        assert slot is not None
        slot.tax_month = 8
        with pytest.raises(DBAPIError, match="identity and regular batch are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        slot = session.get(PayrollTaxStateSlot, slot_id)
        assert slot is not None
        slot.final_batch_id = regular_b_id
        with pytest.raises(DBAPIError, match="combined annual bonus"):
            session.commit()

    with Session(postgres_engine) as session:
        slot = session.get(PayrollTaxStateSlot, slot_id)
        assert slot is not None
        session.delete(slot)
        with pytest.raises(DBAPIError, match="tax state slot|requires exactly one"):
            session.commit()


def test_r2_005_final_vouchers_and_business_events_are_database_immutable(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R2-005"
        )
        event = _event(session, organization.id, "r2-005-empty")
        session.add(
            Voucher(
                org_id=organization.id,
                event_id=event.id,
                voucher_number="202508-r2-empty",
                posting_date=date(2026, 2, 28),
                description="empty",
                status="posted",
            )
        )
        with pytest.raises(DBAPIError, match="at least two balanced nonzero"):
            session.commit()

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R2-005-event"
        )
        original = _event(session, organization.id, "r2-005-original")
        original_voucher = create_voucher(
            session,
            event=original,
            posting_date=date(2026, 2, 28),
            description="正式事件",
            entries=[
                Entry(account_role="bank", debit_fen=1),
                Entry(account_role="service_revenue", credit_fen=1),
            ],
        )
        original.status = "posted"
        session.commit()
        original_id = original.id
        organization_id = organization.id

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, original_id)
        assert original is not None
        original.facts = {"tampered": True}
        with pytest.raises(DBAPIError, match="final business events are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, original_id)
        assert original is not None
        reversal = _event(session, organization_id, "r2-005-reversal")
        reversal.event_type = "reversal"
        reversal.facts = {"original_event_id": str(original.id)}
        create_voucher(
            session,
            event=reversal,
            posting_date=date(2026, 2, 28),
            description="正式冲正",
            entries=[
                Entry(account_role="service_revenue", debit_fen=1),
                Entry(account_role="bank", credit_fen=1),
            ],
            reversal_of=original_voucher,
        )
        reversal.status = "posted"
        original.status = "reversed"
        original.reversed_by_event_id = reversal.id
        session.commit()


def test_r2_006_voucher_line_composite_organization_foreign_keys(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session, accounting_period_control_enabled=False, name="R2-006-A"
        )
        organization_b = seed_organization(
            session, accounting_period_control_enabled=False, name="R2-006-B"
        )
        event_a = _event(session, organization_a.id, "r2-006-event-a")
        event_b = _event(session, organization_b.id, "r2-006-event-b")
        voucher_a = Voucher(
            org_id=organization_a.id,
            event_id=event_a.id,
            voucher_number="202508-r2-006-a",
            posting_date=date(2026, 2, 28),
            description="draft",
            status="draft",
        )
        voucher_b = Voucher(
            org_id=organization_b.id,
            event_id=event_b.id,
            voucher_number="202508-r2-006-b",
            posting_date=date(2026, 2, 28),
            description="draft",
            status="draft",
        )
        foreign_counterparty = Counterparty(
            org_id=organization_b.id, kind="supplier", name="R2-006 foreign"
        )
        session.add_all([voucher_a, voucher_b, foreign_counterparty])
        session.flush()
        organization_a_id = organization_a.id
        organization_b_id = organization_b.id
        voucher_a_id = voucher_a.id
        voucher_b_id = voucher_b.id
        foreign_counterparty_id = foreign_counterparty.id
        session.commit()

    with Session(postgres_engine) as session:
        foreign_account = get_account_by_role(session, organization_b_id, "bank")
        session.add(
            VoucherLine(
                org_id=organization_a_id,
                voucher_id=voucher_a_id,
                line_number=1,
                account_id=foreign_account.id,
                debit_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        local_account = get_account_by_role(session, organization_a_id, "bank")
        session.add(
            VoucherLine(
                org_id=organization_a_id,
                voucher_id=voucher_a_id,
                line_number=1,
                account_id=local_account.id,
                counterparty_id=foreign_counterparty_id,
                debit_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        local_account = get_account_by_role(session, organization_a_id, "bank")
        session.add(
            VoucherLine(
                org_id=organization_a_id,
                voucher_id=voucher_b_id,
                line_number=1,
                account_id=local_account.id,
                debit_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_r2_010_explicit_version_successors_allow_only_their_own_overlap(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R2-010"
        )
        organization_id = organization.id
        policy = _policy(session, organization.id, version="r2-010-root")
        employee, profile = _employee_profile(session, organization.id, code="R2-010")
        profile_successor = EmployeePayrollProfileVersion(
            org_id=organization.id,
            employee_id=employee.id,
            supersedes_id=profile.id,
            effective_from=profile.effective_from,
            expense_role=profile.expense_role,
            social_insurance_base_fen=0,
            housing_fund_base_fen=0,
            resident_employee=True,
        )
        policy_successor = PayrollPolicyVersion(
            org_id=organization.id,
            region=policy.region,
            supersedes_id=policy.id,
            effective_from=policy.effective_from,
            version="r2-010-successor",
            source_url=policy.source_url,
            parameters=policy.parameters,
        )
        opening = PayrollOpeningState(
            org_id=organization.id,
            employee_id=employee.id,
            tax_year=2026,
            through_month=7,
        )
        session.add_all([profile_successor, policy_successor, opening])
        session.flush()
        session.add(
            PayrollOpeningState(
                org_id=organization.id,
                employee_id=employee.id,
                tax_year=2026,
                through_month=7,
                supersedes_id=opening.id,
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.org_id == organization_id))
        assert employee is not None
        session.add(
            EmployeePayrollProfileVersion(
                org_id=organization_id,
                employee_id=employee.id,
                effective_from=date(2025, 7, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=0,
                housing_fund_base_fen=0,
                resident_employee=True,
            )
        )
        with pytest.raises(DBAPIError, match="NON_ANCESTOR_OVERLAP|explicit supersession"):
            session.commit()


def test_r2_014_postgresql_legacy_settlement_pollution_keeps_revision_0001() -> None:
    """The preflight is direct SQL and fails before migration DDL is applied."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "0001_initial")
        engine = create_engine(url)
        organization_id = uuid.uuid4()
        counterparty_id = uuid.uuid4()
        event_id = uuid.uuid4()
        item_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO organizations (
                            id, name, taxpayer_type, filing_cycle, jurisdiction,
                            urban_maintenance_rate, accounting_standard, created_at
                        ) VALUES (:id, 'R2-014', 'small_scale', 'quarterly', 'CN', 0.07,
                                  'small_enterprise', now())
                        """
                    ),
                    {"id": organization_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO counterparties (id, org_id, kind, name) "
                        "VALUES (:id, :org_id, 'supplier', 'legacy supplier')"
                    ),
                    {"id": counterparty_id, "org_id": organization_id},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO business_events (
                            id, org_id, idempotency_key, event_type, status, description,
                            facts, business_date, posting_date, rule_trace, created_at
                        ) VALUES (:id, :org_id, 'legacy-r2-014', 'legacy', 'posted', '',
                                  CAST('{}' AS jsonb), DATE '2026-02-01', DATE '2026-02-01',
                                  CAST('[]' AS jsonb), now())
                        """
                    ),
                    {"id": event_id, "org_id": organization_id},
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO open_items (
                            id, org_id, counterparty_id, source_event_id, item_type,
                            original_amount_fen, settled_amount_fen, status
                        ) VALUES (
                            :id, :org_id, :counterparty_id, :event_id, 'payable', 100, 0, 'open'
                        )
                        """
                    ),
                    {
                        "id": item_id,
                        "org_id": organization_id,
                        "counterparty_id": counterparty_id,
                        "event_id": event_id,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO settlements (
                            id, org_id, open_item_id, payment_event_id, amount_fen, reversed
                        )
                        VALUES (:id, :org_id, :open_item_id, :event_id, 1, false)
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org_id": organization_id,
                        "open_item_id": item_id,
                        "event_id": event_id,
                    },
                )
            with pytest.raises(RuntimeError, match="OPEN_ITEM_SETTLEMENT_INVARIANT_VIOLATION"):
                command.upgrade(config, "head")
            with engine.connect() as connection:
                revision = connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                assert revision == "0001_initial"
        finally:
            engine.dispose()
