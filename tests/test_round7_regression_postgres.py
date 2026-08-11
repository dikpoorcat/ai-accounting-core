"""Independent PostgreSQL 17 regressions for the R7 migration boundary.

The historical-upgrade cases deliberately start at their named Alembic
revision.  They never create a schema from ORM metadata and never use a
head-upgraded fixture as a substitute for a historical database.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_round4_event_integrity_postgres import (
    _salary_payment_with_unsettled_statutory_sources,
)
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.models import BusinessEvent, OpenItem, Settlement
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService
from alembic import command

REVISION_0005 = "0005_payroll_round4_integrity"
REVISION_0007 = "0007_payroll_round6_closure"
REVISION_0008 = "0008_payroll_r7_tax_closure"
REVISION_HEAD = "0012_accounting_period_close"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@contextmanager
def _postgres_at(revision: str) -> Iterator[tuple[Config, object]]:
    """Create an isolated PostgreSQL 17 database at exactly ``revision``."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = _config(database_url)
        command.upgrade(config, revision)
        engine = create_engine(database_url)
        try:
            yield config, engine
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    """A current-head temporary database only for canonical service paths."""

    with _postgres_at("head") as (config, engine):
        command.check(config)
        yield engine


def _revision(engine: object) -> str:
    with engine.connect() as connection:
        return connection.scalar(sa.text("SELECT version_num FROM alembic_version"))


def _salary_payment_settlement(session: Session, *, key: str) -> dict[str, uuid.UUID]:
    """Build one canonical salary-payment settlement through the service."""

    identifiers = _salary_payment_with_unsettled_statutory_sources(session, key=key)
    salary_event_id = session.scalar(
        select(OpenItem.source_event_id).where(
            OpenItem.org_id == identifiers["org_id"],
            OpenItem.id == identifiers["withheld_social_item_id"],
        )
    )
    assert salary_event_id is not None
    settlement = session.scalar(
        select(Settlement).where(
            Settlement.org_id == identifiers["org_id"],
            Settlement.payment_event_id == salary_event_id,
            Settlement.reversed.is_(False),
        )
    )
    assert settlement is not None
    return {
        "org_id": identifiers["org_id"],
        "salary_event_id": salary_event_id,
        "settlement_id": settlement.id,
    }


def _settlement_row(engine: object, settlement_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT id, org_id, open_item_id, payment_event_id, amount_fen, reversed, "
                    "reversed_by_event_id FROM settlements WHERE id = :settlement_id"
                ),
                {"settlement_id": settlement_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_r7_004_combined_settlement_id_and_amount_mutation_rolls_back_full_row(
    postgres_engine: object,
) -> None:
    """One UPDATE cannot evade the source-edge closure by changing a row's identity."""

    with Session(postgres_engine) as session:
        identifiers = _salary_payment_settlement(session, key="r7-combined-settlement")
        session.commit()

    before = _settlement_row(postgres_engine, identifiers["settlement_id"])
    with Session(postgres_engine) as session:
        with pytest.raises(DBAPIError, match="R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE"):
            session.execute(
                sa.text(
                    "UPDATE settlements SET id = :replacement_id, amount_fen = amount_fen + 1 "
                    "WHERE id = :settlement_id"
                ),
                {**identifiers, "replacement_id": uuid.uuid4()},
            )
            session.commit()
        session.rollback()

    assert _settlement_row(postgres_engine, identifiers["settlement_id"]) == before


def test_r7_004_canonical_service_reversal_marks_exact_settlement_reversal_id(
    postgres_engine: object,
) -> None:
    """The public reversal path writes exactly its own auditable settlement link."""

    with Session(postgres_engine) as session:
        identifiers = _salary_payment_settlement(session, key="r7-canonical-reversal")
        original_settlement_ids = set(
            session.scalars(
                select(Settlement.id).where(
                    Settlement.org_id == identifiers["org_id"],
                    Settlement.payment_event_id == identifiers["salary_event_id"],
                )
            ).all()
        )
        assert original_settlement_ids == {identifiers["settlement_id"]}
        result = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=identifiers["org_id"],
                event_id=identifiers["salary_event_id"],
                idempotency_key="r7-canonical-salary-reversal",
                reason="R7 规范结算冲正",
                posting_date=date(2026, 3, 6),
            )
        )
        assert result.status == "posted", result.errors
        assert result.event_id is not None
        reversal_event_id = result.event_id
        session.commit()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, identifiers["salary_event_id"])
        reversal = session.get(BusinessEvent, reversal_event_id)
        settlements = session.scalars(
            select(Settlement).where(
                Settlement.org_id == identifiers["org_id"],
                Settlement.payment_event_id == identifiers["salary_event_id"],
            )
        ).all()
        assert original is not None and original.status == "reversed"
        assert original.reversed_by_event_id == reversal_event_id
        assert reversal is not None and reversal.status == "posted"
        assert reversal.facts["original_event_id"] == str(identifiers["salary_event_id"])
        assert {settlement.id for settlement in settlements} == original_settlement_ids
        assert all(settlement.reversed for settlement in settlements)
        assert {settlement.reversed_by_event_id for settlement in settlements} == {
            reversal_event_id
        }


def _final_regular_batch(engine: object, *, key: str) -> dict[str, uuid.UUID]:
    """Create a real final regular batch without any post-head fixture data."""

    ids = {
        name: uuid.uuid4()
        for name in (
            "org_id",
            "counterparty_id",
            "employee_id",
            "profile_id",
            "policy_id",
            "batch_id",
            "line_id",
        )
    }
    with engine.begin() as connection:
        connection.execute(sa.text("SET LOCAL session_replication_role = replica"))
        connection.execute(
            sa.text(
                "INSERT INTO organizations (id, name, taxpayer_type, filing_cycle, jurisdiction, urban_maintenance_rate, accounting_standard, created_at) VALUES (:org_id, :name, 'small_scale', 'quarterly', 'CN', 0.07, 'small_enterprise', now())"  # noqa: E501
            ),
            {**ids, "name": f"R7 历史预检 {key}"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO counterparties (id, org_id, kind, name) VALUES (:counterparty_id, :org_id, 'employee', 'R7 employee')"  # noqa: E501
            ),
            ids,
        )
        connection.execute(
            sa.text(
                "INSERT INTO employees (id, org_id, counterparty_id, employee_code, name, employment_start_date, status, created_at) VALUES (:employee_id, :org_id, :counterparty_id, :code, 'R7 employee', '2026-01-01', 'active', now())"  # noqa: E501
            ),
            {**ids, "code": key},
        )
        connection.execute(
            sa.text(
                "INSERT INTO employee_payroll_profile_versions (id, org_id, employee_id, supersedes_id, effective_from, effective_to, expense_role, social_insurance_base_fen, housing_fund_base_fen, resident_employee, created_at) VALUES (:profile_id, :org_id, :employee_id, NULL, '2026-01-01', '2026-12-31', 'payroll_management_expense', 1000000, 1000000, TRUE, now())"  # noqa: E501
            ),
            ids,
        )
        connection.execute(
            sa.text(
                "INSERT INTO payroll_policy_versions (id, org_id, region, supersedes_id, effective_from, effective_to, version, source_url, parameters, created_at) VALUES (:policy_id, :org_id, 'CN', NULL, '2026-01-01', '2026-12-31', :version, 'https://www.chinatax.gov.cn/', '{}'::jsonb, now())"  # noqa: E501
            ),
            {**ids, "version": key},
        )
        connection.execute(
            sa.text(
                "INSERT INTO payroll_batches (id, org_id, idempotency_key, batch_kind, payroll_period, version, status, calculation_hash, calculation_input, calculation_trace, policy_snapshot, policy_version_id, posting_date, payment_date, confirmed_by, confirmed_at, created_at) VALUES (:batch_id, :org_id, :key, 'regular', '2026-03', 1, 'posted', :hash, '{}'::jsonb, '[]'::jsonb, '{}'::jsonb, :policy_id, '2026-03-05', '2026-03-05', 'legacy', now(), now())"  # noqa: E501
            ),
            {**ids, "key": key, "hash": (key * 64)[:64]},
        )
        connection.execute(
            sa.text(
                "INSERT INTO payroll_lines (id, org_id, payroll_batch_id, employee_id, employee_payroll_profile_version_id, base_salary_fen, performance_pay_fen, taxable_allowance_fen, tax_exempt_income_fen, attendance_deduction_fen, special_additional_deduction_fen, other_legal_deduction_fen, annual_bonus_fen, employee_social_insurance_fen, employer_social_insurance_fen, employee_housing_fund_fen, employer_housing_fund_fen, employee_social_insurance_items, employer_social_insurance_items, employee_housing_fund_items, employer_housing_fund_items, individual_income_tax_fen, gross_salary_fen, net_salary_fen, calculation_trace) VALUES (:line_id, :org_id, :batch_id, :employee_id, :profile_id, 100, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 0, 100, 100, '[]'::jsonb)"  # noqa: E501
            ),
            ids,
        )
    return {name: ids[name] for name in ("org_id", "employee_id", "batch_id", "profile_id")}


def _preflight_snapshot(engine: object, *, employee_id: uuid.UUID) -> dict[str, object]:
    with engine.connect() as connection:
        profiles = (
            connection.execute(
                sa.text(
                    "SELECT id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                    "social_insurance_base_fen, housing_fund_base_fen "
                    "FROM employee_payroll_profile_versions WHERE employee_id = :employee_id "
                    "ORDER BY id"
                ),
                {"employee_id": employee_id},
            )
            .mappings()
            .all()
        )
        batches = (
            connection.execute(
                sa.text(
                    "SELECT id, org_id, status, reversal_of_batch_id, policy_version_id, "
                    "payment_date "
                    "FROM payroll_batches "
                    "ORDER BY id"
                )
            )
            .mappings()
            .all()
        )
    return {
        "revision": _revision(engine),
        "profiles": [dict(row) for row in profiles],
        "batches": [dict(row) for row in batches],
    }


def test_r7_004_0007_to_0008_preflight_preserves_revision_and_polluted_rows() -> None:
    """The R7 preflight refuses a historical successor before installing 0008."""

    with _postgres_at(REVISION_0007) as (config, engine):
        identifiers = _final_regular_batch(engine, key="r7-0007-pollution")

        # This is deliberately legacy/direct-SQL pollution.  At 0007 the
        # normal trigger blocks it; disabling that trigger simulates data that
        # predates the R7 tax-downstream closure and tests upgrade atomicity.
        with engine.begin() as connection:
            connection.execute(
                sa.text("ALTER TABLE employee_payroll_profile_versions DISABLE TRIGGER ALL")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO employee_payroll_profile_versions "
                    "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                    "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                    "resident_employee, created_at) "
                    "VALUES (:id, :org_id, :employee_id, :profile_id, '2026-03-01', "
                    "'2026-03-31', 'payroll_management_expense', 1000001, 1000001, TRUE, now())"
                ),
                {**identifiers, "id": uuid.uuid4()},
            )
            connection.execute(
                sa.text("ALTER TABLE employee_payroll_profile_versions ENABLE TRIGGER ALL")
            )

        before = _preflight_snapshot(engine, employee_id=identifiers["employee_id"])
        assert before["revision"] == REVISION_0007
        with pytest.raises(RuntimeError, match="R7_FINAL_PAYROLL_TAX_DOWNSTREAM_PRECHECK_FAILED"):
            command.upgrade(config, REVISION_0008)
        assert _preflight_snapshot(engine, employee_id=identifiers["employee_id"]) == before


def _insert_historical_organization(engine: object, *, name: str) -> uuid.UUID:
    organization_id = uuid.uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO organizations "
                "(id, name, taxpayer_type, filing_cycle, jurisdiction, urban_maintenance_rate, "
                "accounting_standard, created_at) "
                "VALUES (:id, :name, 'small_scale', 'quarterly', 'CN', 0.07, "
                "'small_enterprise', :created_at)"
            ),
            {"id": organization_id, "name": name, "created_at": datetime.now(UTC)},
        )
    return organization_id


@pytest.mark.parametrize("starting_revision", (REVISION_0005, REVISION_0007))
def test_r7_004_exact_historical_revision_upgrades_to_head_without_head_fixture(
    starting_revision: str,
) -> None:
    """Both supported historical states upgrade directly through every missing revision."""

    with _postgres_at(starting_revision) as (config, engine):
        assert _revision(engine) == starting_revision
        if starting_revision == REVISION_0005:
            assert "payroll_version_guards" not in inspect(engine).get_table_names()
        organization_id = _insert_historical_organization(
            engine, name=f"R7 历史迁移 {starting_revision}"
        )

        command.upgrade(config, "head")
        command.check(config)

        assert _revision(engine) == REVISION_HEAD
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT id FROM organizations WHERE id = :organization_id"),
                    {"organization_id": organization_id},
                )
                == organization_id
            )


def test_r7_004_empty_head_downgrade_and_reupgrade_remain_revision_exact() -> None:
    """An empty temporary database can traverse the R7 migration boundary both ways."""

    with _postgres_at("head") as (config, engine):
        command.check(config)
        assert _revision(engine) == REVISION_HEAD

        command.downgrade(config, REVISION_0007)
        assert _revision(engine) == REVISION_0007

        command.upgrade(config, "head")
        command.check(config)
        assert _revision(engine) == REVISION_HEAD
