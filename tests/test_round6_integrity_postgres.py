"""R6 direct PostgreSQL 17 attacks for final payroll closures.

The cases intentionally construct mutations below ``FinanceService`` where
that is the point of the remediation: a green public precondition must never
be required for the accounting database to remain coherent.
"""

from __future__ import annotations

import base64
import calendar
import shutil
import threading
import uuid
from collections.abc import Iterator
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.config import Config
from conftest import prepare_authenticated_bank_account
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_payroll_service import add_bank_row, register_payroll_facts
from test_round4_event_integrity_postgres import (
    _confirmed_payroll_with_evidence,
)
from test_round5_provenance_postgres import _post_regular_tax_source
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.evidence import register_evidence
from ai_accounting.ledger import Entry, create_voucher
from ai_accounting.models import (
    BankTransactionMatch,
    BusinessEvent,
    EmployeePayrollProfileVersion,
    Evidence,
    OpenItem,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollOpeningState,
    PayrollPolicyVersion,
    Settlement,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEvidenceRequest,
    RegisterPayrollOpeningStateRequest,
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
    """A blank current-head PostgreSQL 17 schema for COMMIT-boundary attacks."""

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
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


@pytest.fixture
def isolated_postgres_engine() -> Iterator[object]:
    """Isolate the owner-mode statutory collection attack from legacy cases."""

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def _commit_rejects(
    engine: object, statement: sa.TextClause, values: dict[str, object], code: str | None
) -> None:
    with Session(engine, expire_on_commit=False) as session:
        with pytest.raises(DBAPIError, match=code):
            session.execute(statement, values)
            session.commit()
        session.rollback()


def _preview(
    session: Session,
    *,
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    payroll_period: str,
    payment_date: date,
    key: str,
) -> object:
    preview = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "batch_kind": "regular",
                "payroll_period": payroll_period,
                "posting_date": payment_date.isoformat(),
                "payment_date": payment_date.isoformat(),
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": 1_000_000,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )
    assert preview.status == "calculated", preview.errors
    return preview


def _confirmed_cross_month_regular(session: Session, *, key: str) -> tuple[object, object, object]:
    """Final October payroll paid in November: profile uses October month-end."""

    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        accounting_period_control_enabled=False,
        name=f"R6 跨月资料 {key}",
    )
    employee_id = register_payroll_facts(session, organization)
    service = FinanceService(session)
    opening = service.register_payroll_opening_state(
        RegisterPayrollOpeningStateRequest(
            org_id=organization.id,
            employee_id=employee_id,
            tax_year=2026,
            through_month=2,
            cumulative_income_fen=0,
            cumulative_tax_exempt_income_fen=0,
            cumulative_basic_deduction_fen=0,
            cumulative_employee_social_insurance_fen=0,
            cumulative_employee_housing_fund_fen=0,
            cumulative_special_additional_deduction_fen=0,
            cumulative_other_legal_deduction_fen=0,
            cumulative_tax_relief_fen=0,
            cumulative_tax_withheld_fen=0,
        )
    )
    assert opening["status"] == "registered", opening
    preview = _preview(
        session,
        org_id=organization.id,
        employee_id=employee_id,
        payroll_period="2026-04",
        payment_date=date(2026, 5, 5),
        key=f"{key}-preview",
    )
    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key=f"{key}-confirm",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    return organization, preview, confirmed


def test_r6_001_final_dependency_closure_blocks_direct_successors_and_period_end_bypass(
    postgres_engine: object,
) -> None:
    """All three direct successors fail over a final October/November payroll."""

    with Session(postgres_engine) as session:
        organization, preview, confirmed = _confirmed_cross_month_regular(
            session, key="direct-successor"
        )
        batch = session.get(PayrollBatch, preview.batch_id)
        line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
        )
        opening = session.scalar(
            select(PayrollOpeningState).where(
                PayrollOpeningState.org_id == organization.id,
                PayrollOpeningState.employee_id == line.employee_id,
                PayrollOpeningState.tax_year == 2026,
                PayrollOpeningState.through_month == 2,
            )
        )
        assert batch is not None and line is not None and opening is not None
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        policy = session.get(PayrollPolicyVersion, batch.policy_version_id)
        assert profile is not None and policy is not None
        ids = {
            "org_id": organization.id,
            "employee_id": line.employee_id,
            "profile_id": profile.id,
            "policy_id": policy.id,
            "opening_id": opening.id,
            "event_id": confirmed.event_id,
        }
        session.commit()

    for statement, error in (
        (
            sa.text(
                "INSERT INTO employee_payroll_profile_versions "
                "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                "resident_employee, created_at) VALUES "
                "(:id, :org_id, :employee_id, :profile_id, '2026-04-01', '2026-04-30', "
                "'payroll_management_expense', 1000001, 1000001, TRUE, now())"
            ),
            "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED",
        ),
        (
            sa.text(
                "INSERT INTO payroll_policy_versions "
                "(id, org_id, region, supersedes_id, effective_from, effective_to, version, "
                "source_url, parameters, created_at) "
                "SELECT :id, :org_id, region, :policy_id, '2026-05-01', '2026-05-31', "
                "'r6-direct-policy', source_url, parameters, now() "
                "FROM payroll_policy_versions WHERE id = :policy_id"
            ),
            "R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED",
        ),
        (
            sa.text(
                "INSERT INTO payroll_opening_states "
                "(id, org_id, employee_id, supersedes_id, tax_year, through_month, "
                "cumulative_income_fen, cumulative_tax_exempt_income_fen, "
                "cumulative_basic_deduction_fen, cumulative_employee_social_insurance_fen, "
                "cumulative_employee_housing_fund_fen, "
                "cumulative_special_additional_deduction_fen, "
                "cumulative_other_legal_deduction_fen, "
                "cumulative_tax_relief_fen, cumulative_tax_withheld_fen, created_at) "
                "SELECT :id, org_id, employee_id, :opening_id, tax_year, through_month, "
                "cumulative_income_fen + 1, cumulative_tax_exempt_income_fen, "
                "cumulative_basic_deduction_fen, cumulative_employee_social_insurance_fen, "
                "cumulative_employee_housing_fund_fen, "
                "cumulative_special_additional_deduction_fen, "
                "cumulative_other_legal_deduction_fen, "
                "cumulative_tax_relief_fen, cumulative_tax_withheld_fen, now() "
                "FROM payroll_opening_states WHERE id = :opening_id"
            ),
            "R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED",
        ),
    ):
        _commit_rejects(postgres_engine, statement, {**ids, "id": uuid.uuid4()}, error)

    # Canonical reversal removes the only final dependency; the durable guard
    # must not leave a stale lock that prevents a compliant reconstruction.
    with Session(postgres_engine) as session:
        reversed_result = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=ids["org_id"],
                event_id=ids["event_id"],
                idempotency_key="r6-direct-successor-reverse",
                reason="R6 资料更正前冲正",
                posting_date=date(2026, 5, 6),
            )
        )
        assert reversed_result.status == "posted", reversed_result.errors
        session.commit()
    with Session(postgres_engine) as session:
        session.execute(
            sa.text(
                "INSERT INTO employee_payroll_profile_versions "
                "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                "resident_employee, created_at) VALUES "
                "(:id, :org_id, :employee_id, :profile_id, '2026-04-01', '2026-04-30', "
                "'payroll_management_expense', 1000001, 1000001, TRUE, now())"
            ),
            {**ids, "id": uuid.uuid4()},
        )
        session.commit()


def test_r6_001_public_profile_correction_uses_period_end_not_payment_date(
    postgres_engine: object,
) -> None:
    """October profile facts stay blocked even when paid in November."""

    with Session(postgres_engine) as session:
        organization, preview, _confirmed = _confirmed_cross_month_regular(
            session, key="public-period-end"
        )
        line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
        )
        assert line is not None
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        assert profile is not None
        result = FinanceService(session).register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=line.employee_id,
                supersedes_profile_version_id=profile.id,
                effective_from=date(2026, 4, 1),
                effective_to=date(2026, 4, 30),
                expense_role=profile.expense_role,
                social_insurance_base_fen=profile.social_insurance_base_fen + 1,
                housing_fund_base_fen=profile.housing_fund_base_fen,
                resident_employee=profile.resident_employee,
            )
        )
        assert result["status"] == "rejected"
        assert result["errors"] == ["PAYROLL_VERSION_CORRECTION_BLOCKED_BY_FINAL_FACTS"]
        session.rollback()


def _unconfirmed_regular_for_correction_race(
    engine: object,
    *,
    key: str,
    payroll_period: str = "2026-03",
    payment_date: date = date(2026, 3, 5),
) -> dict[str, object]:
    """Persist one old-version draft that two isolated sessions can race over."""

    with Session(engine, expire_on_commit=False) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name=f"R6 版本并发 {key}",
        )
        employee_id = register_payroll_facts(session, organization)
        service = FinanceService(session)
        opening = service.register_payroll_opening_state(
            RegisterPayrollOpeningStateRequest(
                org_id=organization.id,
                employee_id=employee_id,
                tax_year=2026,
                through_month=2,
                cumulative_income_fen=0,
                cumulative_tax_exempt_income_fen=0,
                cumulative_basic_deduction_fen=0,
                cumulative_employee_social_insurance_fen=0,
                cumulative_employee_housing_fund_fen=0,
                cumulative_special_additional_deduction_fen=0,
                cumulative_other_legal_deduction_fen=0,
                cumulative_tax_relief_fen=0,
                cumulative_tax_withheld_fen=0,
            )
        )
        assert opening["status"] == "registered", opening
        preview = _preview(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period=payroll_period,
            payment_date=payment_date,
            key=f"{key}-preview",
        )
        line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
        )
        batch = session.get(PayrollBatch, preview.batch_id)
        assert line is not None and batch is not None
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        policy = session.get(PayrollPolicyVersion, batch.policy_version_id)
        opening_row = session.get(PayrollOpeningState, uuid.UUID(opening["opening_state_id"]))
        assert profile is not None and policy is not None and opening_row is not None
        session.commit()
        return {
            "org_id": organization.id,
            "employee_id": employee_id,
            "preview": preview,
            "profile": profile,
            "policy": policy,
            "opening": opening_row,
            "profile_effective_from": date.fromisoformat(f"{payroll_period}-01"),
            "profile_effective_to": date(
                int(payroll_period[:4]),
                int(payroll_period[5:]),
                calendar.monthrange(int(payroll_period[:4]), int(payroll_period[5:]))[1],
            ),
        }


def _successor_request(kind: str, facts: dict[str, object], *, key: str) -> object:
    """Build a public correction that changes the version used by September."""

    org_id = facts["org_id"]
    employee_id = facts["employee_id"]
    if kind == "profile":
        profile = facts["profile"]
        assert isinstance(profile, EmployeePayrollProfileVersion)
        effective_from = facts["profile_effective_from"]
        effective_to = facts["profile_effective_to"]
        assert isinstance(effective_from, date) and isinstance(effective_to, date)
        return RegisterEmployeePayrollProfileVersionRequest(
            org_id=org_id,
            employee_id=employee_id,
            supersedes_profile_version_id=profile.id,
            effective_from=effective_from,
            effective_to=effective_to,
            expense_role=profile.expense_role,
            social_insurance_base_fen=profile.social_insurance_base_fen + 1,
            housing_fund_base_fen=profile.housing_fund_base_fen,
            resident_employee=profile.resident_employee,
        )
    if kind == "policy":
        policy = facts["policy"]
        assert isinstance(policy, PayrollPolicyVersion)
        return RegisterPayrollPolicyVersionRequest(
            org_id=org_id,
            region=policy.region,
            supersedes_policy_version_id=policy.id,
            effective_from=date(2026, 3, 1),
            effective_to=date(2026, 3, 31),
            version=f"r6-race-{key}",
            source_url=policy.source_url,
            parameters=policy.parameters,
        )
    opening = facts["opening"]
    assert isinstance(opening, PayrollOpeningState)
    return RegisterPayrollOpeningStateRequest(
        org_id=org_id,
        employee_id=employee_id,
        supersedes_opening_state_id=opening.id,
        tax_year=opening.tax_year,
        through_month=opening.through_month,
        cumulative_income_fen=opening.cumulative_income_fen + 1,
        cumulative_tax_exempt_income_fen=opening.cumulative_tax_exempt_income_fen,
        cumulative_basic_deduction_fen=opening.cumulative_basic_deduction_fen,
        cumulative_employee_social_insurance_fen=opening.cumulative_employee_social_insurance_fen,
        cumulative_employee_housing_fund_fen=opening.cumulative_employee_housing_fund_fen,
        cumulative_special_additional_deduction_fen=(
            opening.cumulative_special_additional_deduction_fen
        ),
        cumulative_other_legal_deduction_fen=opening.cumulative_other_legal_deduction_fen,
        cumulative_tax_relief_fen=opening.cumulative_tax_relief_fen,
        cumulative_tax_withheld_fen=opening.cumulative_tax_withheld_fen,
    )


@pytest.mark.parametrize("kind", ("profile", "policy", "opening"))
@pytest.mark.parametrize("confirmation_commits_first", (True, False))
def test_r6_001_public_correction_and_confirmation_are_serialized_by_persistent_guards(
    postgres_engine: object,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    confirmation_commits_first: bool,
) -> None:
    """Two independently synchronized sessions cannot both commit old/new facts.

    The correction is deliberately paused immediately *after* its legacy
    public precheck reads an empty final-fact set.  This reproduces the R6
    write-skew rather than relying on a timing loop.  The second order pauses
    confirmation after recalculation, while it still holds the old profile /
    policy / opening selection in memory.
    """

    facts = _unconfirmed_regular_for_correction_race(
        postgres_engine,
        key=f"{kind}-{'confirm' if confirmation_commits_first else 'correction'}",
        # Payroll profile selection is keyed by period-end, deliberately
        # independent of the payment-date tax month.  Run the two profile
        # races across that boundary rather than only in a same-month sample.
        payroll_period="2026-04" if kind == "profile" else "2026-03",
        payment_date=date(2026, 5, 5) if kind == "profile" else date(2026, 3, 5),
    )
    checked = threading.Event()
    release_correction = threading.Event()
    confirmation_ready = threading.Event()
    release_confirmation = threading.Event()
    correction_finished = threading.Event()
    outcomes: dict[str, object] = {}
    blocker_name = {
        "profile": "_profile_correction_blocking_batches",
        "policy": "_policy_correction_blocking_batches",
        "opening": "_opening_correction_blocking_batches",
    }[kind]
    original_blocker = getattr(FinanceService, blocker_name)
    original_entitlements = FinanceService._create_payroll_withholding_entitlements

    def paused_blocker(service: FinanceService, *args: object, **kwargs: object) -> set[uuid.UUID]:
        result = original_blocker(service, *args, **kwargs)
        if threading.current_thread().name == "r6-correction":
            checked.set()
            assert release_correction.wait(15), "correction barrier was never released"
        return result

    def paused_entitlements(
        service: FinanceService, batch: PayrollBatch, lines: list[PayrollLine]
    ) -> None:
        if not confirmation_commits_first and threading.current_thread().name == "r6-confirmation":
            confirmation_ready.set()
            assert release_confirmation.wait(15), "confirmation barrier was never released"
        original_entitlements(service, batch, lines)

    monkeypatch.setattr(FinanceService, blocker_name, paused_blocker)
    monkeypatch.setattr(
        FinanceService, "_create_payroll_withholding_entitlements", paused_entitlements
    )

    def correction_worker() -> None:
        try:
            with Session(postgres_engine) as session:
                service = FinanceService(session)
                request = _successor_request(kind, facts, key="public-race")
                register = {
                    "profile": service.register_employee_payroll_profile_version,
                    "policy": service.register_payroll_policy_version,
                    "opening": service.register_payroll_opening_state,
                }[kind]
                outcomes["correction"] = register(request)
                session.commit()
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["correction_error"] = exc
        finally:
            correction_finished.set()

    def confirmation_worker() -> None:
        try:
            preview = facts["preview"]
            assert hasattr(preview, "batch_id") and hasattr(preview, "calculation_hash")
            with Session(postgres_engine) as session:
                outcomes["confirmation"] = FinanceService(session).confirm_payroll(
                    ConfirmPayrollRequest(
                        org_id=facts["org_id"],
                        batch_id=preview.batch_id,
                        calculation_hash=preview.calculation_hash,
                        idempotency_key=f"r6-{kind}-confirmation",
                    )
                )
                session.commit()
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes["confirmation_error"] = exc

    correction = threading.Thread(target=correction_worker, name="r6-correction")
    confirmation = threading.Thread(target=confirmation_worker, name="r6-confirmation")
    correction.start()
    assert checked.wait(15), (
        f"correction did not pass its old public precheck: {outcomes.get('correction_error')!r}"
    )
    confirmation.start()
    if confirmation_commits_first:
        confirmation.join(15)
        assert not confirmation.is_alive(), "confirmation did not complete first"
        release_correction.set()
    else:
        assert confirmation_ready.wait(15), "confirmation did not recalculate old facts"
        release_correction.set()
        assert correction_finished.wait(15), "correction did not complete first"
        release_confirmation.set()
    correction.join(15)
    confirmation.join(15)
    assert not correction.is_alive() and not confirmation.is_alive()
    assert "correction_error" not in outcomes
    assert "confirmation_error" not in outcomes
    correction_result = outcomes["correction"]
    confirmation_result = outcomes["confirmation"]
    assert isinstance(correction_result, dict)
    assert hasattr(confirmation_result, "status")
    if confirmation_commits_first:
        assert confirmation_result.status == "posted", confirmation_result.errors
        assert correction_result["status"] == "rejected"
        assert correction_result["errors"] == ["PAYROLL_VERSION_CORRECTION_BLOCKED_BY_FINAL_FACTS"]
    else:
        assert correction_result["status"] == "registered", correction_result
        assert confirmation_result.status == "rejected"
        assert confirmation_result.errors == ["PAYROLL_CONCURRENT_WRITE_CONFLICT"]


def test_r6_001_direct_update_cannot_move_a_draft_successor_over_final_profile_facts(
    postgres_engine: object,
) -> None:
    """An UPDATE path is no less constrained than a direct successor INSERT."""

    facts = _unconfirmed_regular_for_correction_race(postgres_engine, key="profile-update")
    profile = facts["profile"]
    assert isinstance(profile, EmployeePayrollProfileVersion)
    successor_id = uuid.uuid4()
    preview = facts["preview"]
    assert hasattr(preview, "batch_id") and hasattr(preview, "calculation_hash")
    with Session(postgres_engine) as session:
        result = FinanceService(session).confirm_payroll(
            ConfirmPayrollRequest(
                org_id=facts["org_id"],
                batch_id=preview.batch_id,
                calculation_hash=preview.calculation_hash,
                idempotency_key="r6-profile-update-confirm",
            )
        )
        assert result.status == "posted", result.errors
        session.commit()
    with Session(postgres_engine) as session:
        session.execute(
            sa.text(
                "INSERT INTO employee_payroll_profile_versions "
                "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                "resident_employee, created_at) "
                "SELECT :successor_id, org_id, employee_id, id, '2026-07-01', '2026-07-31', "
                "expense_role, social_insurance_base_fen + 1, housing_fund_base_fen, "
                "resident_employee, now() "
                "FROM employee_payroll_profile_versions WHERE id = :profile_id"
            ),
            {"successor_id": successor_id, "profile_id": profile.id},
        )
        session.commit()
    _commit_rejects(
        postgres_engine,
        sa.text(
            "UPDATE employee_payroll_profile_versions "
            "SET effective_from = '2026-03-01', effective_to = '2026-03-31' "
            "WHERE id = :successor_id"
        ),
        {"successor_id": successor_id},
        "payroll version rows are immutable",
    )


def test_r6_002_r6_004_sealed_evidence_freezes_timestamp_and_requires_lower_hex(
    postgres_engine: object, tmp_path: object
) -> None:
    """Timestamp is sealed, while drafts and service hash idempotency remain valid."""

    with Session(postgres_engine) as session:
        organization, _batch, _line, evidence, _event = _confirmed_payroll_with_evidence(
            session, key="r6-evidence"
        )
        values = {"org_id": organization.id, "evidence_id": evidence.id}
        session.commit()

    _commit_rejects(
        postgres_engine,
        sa.text(
            "UPDATE evidence SET created_at = created_at + INTERVAL '1 second' "
            "WHERE id = :evidence_id"
        ),
        values,
        "R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE",
    )
    _commit_rejects(
        postgres_engine,
        sa.text("UPDATE evidence SET id = :replacement_id WHERE id = :evidence_id"),
        {**values, "replacement_id": uuid.uuid4()},
        "R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE",
    )
    _commit_rejects(
        postgres_engine,
        sa.text(
            "UPDATE evidence SET created_at = created_at + INTERVAL '1 second', "
            "source = 'tampered' WHERE id = :evidence_id"
        ),
        values,
        "R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE",
    )
    invalid_hashes = ("z" * 64, "A" * 64, " " + "a" * 63, "é" * 32, "a" * 63, "a" * 65)
    for invalid_hash in invalid_hashes:
        _commit_rejects(
            postgres_engine,
            sa.text(
                "INSERT INTO evidence "
                "(id, org_id, sha256, original_name, media_type, source, size_bytes, "
                "storage_path, metadata, created_at) "
                "VALUES (:id, :org_id, :sha256, 'bad.txt', 'text/plain', 'r6', 1, "
                "'/r6/bad', '{}'::json, now())"
            ),
            {"id": uuid.uuid4(), "org_id": values["org_id"], "sha256": invalid_hash},
            "ck_evidence_sha256" if len(invalid_hash) == 64 else None,
        )

    with Session(postgres_engine) as session:
        draft = Evidence(
            org_id=values["org_id"],
            sha256="b" * 64,
            original_name="draft.txt",
            media_type="text/plain",
            source="r6",
            size_bytes=1,
            storage_path="/r6/draft",
            metadata_json={},
        )
        session.add(draft)
        session.flush()
        draft.created_at = draft.created_at.replace(year=2026)
        other_organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R6 跨企业摘要",
        )
        # The digest identity is enterprise-scoped, so the same canonical
        # content hash remains legitimate in a different enterprise.
        session.add(
            Evidence(
                org_id=other_organization.id,
                sha256=draft.sha256,
                original_name="same-content.txt",
                media_type="text/plain",
                source="r6",
                size_bytes=1,
                storage_path="/r6/other-enterprise",
                metadata_json={},
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        request = RegisterEvidenceRequest(
            org_id=values["org_id"],
            source="r6-register",
            content_base64=base64.b64encode(b"round-6-lower-hex").decode("ascii"),
            original_name="r6.txt",
        )
        settings = Settings(finance_evidence_dir=tmp_path)
        first = register_evidence(session, request, settings)
        second = register_evidence(session, request, settings)
        assert first.id == second.id
        assert first.sha256 == first.sha256.lower() and len(first.sha256) == 64
        session.commit()


def _stage_direct_tax_payment(
    session: Session,
    *,
    organization: object,
    source_items: list[OpenItem],
    key: str,
) -> BusinessEvent:
    """Make individually canonical tax-source edges, bypassing service collection checks."""

    amount = sum(item.original_amount_fen - item.settled_amount_fen for item in source_items)
    assert amount > 0
    bank = add_bank_row(
        session,
        organization,
        -amount,
        f"{key}-bank",
        booking_date=date(2026, 4, 6),
    )
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key=key,
        event_type="individual_income_tax_payment",
        status="draft",
        description="R6 直接构造法定付款集合",
        facts={
            "amounts": {"amount_fen": amount, "currency": "CNY"},
            "business_dates": {
                "business_date": "2026-04-06",
                "payment_date": "2026-04-06",
                "posting_date": "2026-04-06",
            },
            "bank_account_code": "1002",
        },
        business_date=date(2026, 4, 6),
        payment_date=date(2026, 4, 6),
        posting_date=date(2026, 4, 6),
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    bank.matched_event_id = event.id
    session.add(
        BankTransactionMatch(
            org_id=organization.id,
            bank_transaction_id=bank.id,
            event_id=event.id,
        )
    )
    create_voucher(
        session,
        event=event,
        posting_date=event.posting_date,
        description=event.description,
        entries=[
            Entry(account_role="individual_income_tax_payable", debit_fen=amount),
            Entry(account_role="bank", credit_fen=amount),
        ],
    )
    for item in source_items:
        source_batch_id = session.scalar(
            select(PayrollEventLink.payroll_batch_id).where(
                PayrollEventLink.org_id == organization.id,
                PayrollEventLink.event_id == item.source_event_id,
                PayrollEventLink.link_kind == "salary_payment",
            )
        )
        assert source_batch_id is not None
        outstanding = item.original_amount_fen - item.settled_amount_fen
        session.add(
            Settlement(
                org_id=organization.id,
                open_item_id=item.id,
                payment_event_id=event.id,
                amount_fen=outstanding,
            )
        )
        item.settled_amount_fen += outstanding
        item.status = "settled"
        session.add(
            PayrollEventLink(
                org_id=organization.id,
                event_id=event.id,
                payroll_batch_id=source_batch_id,
                source_payment_event_id=item.source_event_id,
                source_open_item_id=item.id,
                link_kind="statutory_payment",
            )
        )
    session.flush()
    event.status = "posted"
    return event


def test_r6_005_direct_cross_period_statutory_collection_rejects_at_commit(
    isolated_postgres_engine: object,
) -> None:
    """Two otherwise-valid source edges cannot merge September and October tax."""

    with Session(isolated_postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R6 法定集合跨期",
        )
        prepare_authenticated_bank_account(session, organization)
        prepare_authenticated_bank_account(session, organization, booking_date=date(2026, 4, 5))
        employee_id = register_payroll_facts(session, organization)
        _september_preview, september_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r6-period-september",
        )
        _october_preview, october_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-04",
            key="r6-period-october",
        )
        event = _stage_direct_tax_payment(
            session,
            organization=organization,
            source_items=[september_tax, october_tax],
            key="r6-direct-cross-period",
        )
        with pytest.raises(DBAPIError, match="R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES"):
            session.commit()
        session.rollback()
        assert event.id is not None
