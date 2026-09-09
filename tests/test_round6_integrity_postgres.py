"""R6 direct PostgreSQL 17 attacks for final payroll closures.

The cases intentionally construct mutations below ``FinanceService`` where
that is the point of the remediation: a green public precondition must never
be required for the accounting database to remain coherent.
"""

from __future__ import annotations

import base64
import calendar
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from conftest import (
    AuthenticatedOwnerAuthority,
    bind_authenticated_bank_account,
    import_test_bank_transaction,
    prepare_authenticated_bank_account,
)
from sqlalchemy import Engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_payroll_service import register_payroll_facts
from test_round5_provenance_postgres import _post_regular_tax_source

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.config import Settings
from ai_accounting.evidence import register_evidence
from ai_accounting.models import (
    BusinessEvent,
    EmployeePayrollProfileVersion,
    Evidence,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollOpeningState,
    PayrollPolicyVersion,
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

pytestmark = pytest.mark.postgres


@dataclass(frozen=True)
class _Runtime:
    engine: Engine
    org_id: uuid.UUID
    evidence_id: uuid.UUID
    authority: AuthenticatedOwnerAuthority


@pytest.fixture
def postgres_engine() -> object:
    """A genuine catalog-authenticated v3 company for one integrity scenario."""

    with authenticated_business_database("finance_company", name="R6 ����������") as values:
        engine, org_id, evidence_id, authority = values
        yield _Runtime(engine, org_id, evidence_id, authority)


@pytest.fixture
def isolated_postgres_engine(postgres_engine: object) -> object:
    return postgres_engine


@contextmanager
def _session(runtime: object, *, expire_on_commit: bool = True):
    assert isinstance(runtime, _Runtime)
    with Session(runtime.engine, expire_on_commit=expire_on_commit) as session:
        session.info["test_postgres_runtime"] = runtime
        bind_authenticated_bank_account(session, runtime.authority)
        with runtime.authority.attributed_call(
            session, tool_name="finance_round6_integrity_fixture"
        ):
            yield session


def _organization(session: Session, runtime: object) -> Organization:
    assert isinstance(runtime, _Runtime)
    organization = session.get(Organization, runtime.org_id)
    assert organization is not None
    return organization


def _commit_rejects(
    engine: object, statement: sa.TextClause, values: dict[str, object], code: str | None
) -> None:
    with _session(engine, expire_on_commit=False) as session:
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
    evidence_id: uuid.UUID,
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
                "evidence_references": [evidence_id],
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


def _confirmed_cross_month_regular(
    session: Session, runtime: object, *, key: str
) -> tuple[object, object, object]:
    """Final October payroll paid in November: profile uses October month-end."""

    organization = _organization(session, runtime)
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
        evidence_id=runtime.evidence_id if isinstance(runtime, _Runtime) else uuid.UUID(int=0),
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

    with _session(postgres_engine) as session:
        organization, preview, confirmed = _confirmed_cross_month_regular(
            session, postgres_engine, key="direct-successor"
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
                "social_insurance_participating, housing_fund_participating, "
                "resident_employee, execution_attribution_id, created_at) VALUES "
                "(:id, :org_id, :employee_id, :profile_id, '2026-04-01', '2026-04-30', "
                "'payroll_management_expense', 1000001, 1000001, TRUE, TRUE, TRUE, "
                "current_setting('finance.execution_attribution_id')::uuid, now())"
            ),
            "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED",
        ),
        (
            sa.text(
                "INSERT INTO payroll_policy_versions "
                "(id, org_id, region, supersedes_id, effective_from, effective_to, version, "
                "source_url, parameters, execution_attribution_id, created_at) "
                "SELECT :id, :org_id, region, :policy_id, '2026-04-01', '2026-04-30', "
                "'r6-direct-policy', source_url, parameters, "
                "current_setting('finance.execution_attribution_id')::uuid, now() "
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
                "cumulative_tax_relief_fen, cumulative_tax_withheld_fen, "
                "execution_attribution_id, created_at) "
                "SELECT :id, org_id, employee_id, :opening_id, tax_year, through_month, "
                "cumulative_income_fen + 1, cumulative_tax_exempt_income_fen, "
                "cumulative_basic_deduction_fen, cumulative_employee_social_insurance_fen, "
                "cumulative_employee_housing_fund_fen, "
                "cumulative_special_additional_deduction_fen, "
                "cumulative_other_legal_deduction_fen, "
                "cumulative_tax_relief_fen, cumulative_tax_withheld_fen, "
                "current_setting('finance.execution_attribution_id')::uuid, now() "
                "FROM payroll_opening_states WHERE id = :opening_id"
            ),
            "R6_FINAL_PAYROLL_OPENING_CORRECTION_BLOCKED",
        ),
    ):
        _commit_rejects(postgres_engine, statement, {**ids, "id": uuid.uuid4()}, error)

    # Canonical reversal removes the only final dependency; the durable guard
    # must not leave a stale lock that prevents a compliant reconstruction.
    with _session(postgres_engine) as session:
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
    with _session(postgres_engine) as session:
        session.execute(
            sa.text(
                "INSERT INTO employee_payroll_profile_versions "
                "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                "social_insurance_participating, housing_fund_participating, "
                "resident_employee, execution_attribution_id, created_at) VALUES "
                "(:id, :org_id, :employee_id, :profile_id, '2026-04-01', '2026-04-30', "
                "'payroll_management_expense', 1000001, 1000001, TRUE, TRUE, TRUE, "
                "current_setting('finance.execution_attribution_id')::uuid, now())"
            ),
            {**ids, "id": uuid.uuid4()},
        )
        session.commit()


def test_r6_001_public_profile_correction_uses_period_end_not_payment_date(
    postgres_engine: object,
) -> None:
    """October profile facts stay blocked even when paid in November."""

    with _session(postgres_engine) as session:
        organization, preview, _confirmed = _confirmed_cross_month_regular(
            session, postgres_engine, key="public-period-end"
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

    with _session(engine, expire_on_commit=False) as session:
        organization = _organization(session, engine)
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
            evidence_id=engine.evidence_id if isinstance(engine, _Runtime) else uuid.UUID(int=0),
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
            with _session(postgres_engine) as session:
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
            with _session(postgres_engine) as session:
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
        release_confirmation.set()
    correction.join(15)
    confirmation.join(15)
    assert not correction.is_alive() and not confirmation.is_alive()
    race_errors = [
        error
        for name, error in outcomes.items()
        if name in {"correction_error", "confirmation_error"}
    ]
    if race_errors:
        assert len(race_errors) == 1
        assert isinstance(race_errors[0], DBAPIError)
        assert "deadlock detected" in str(race_errors[0])
        assert outcomes.get("confirmation").status == "posted"
        return
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
        outcomes_by_status = {correction_result["status"], confirmation_result.status}
        assert outcomes_by_status == {"registered", "rejected"}
        if correction_result["status"] == "rejected":
            assert correction_result["errors"] == [
                "PAYROLL_VERSION_CORRECTION_BLOCKED_BY_FINAL_FACTS"
            ]
        else:
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
    with _session(postgres_engine) as session:
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
    with _session(postgres_engine) as session:
        session.execute(
            sa.text(
                "INSERT INTO employee_payroll_profile_versions "
                "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
                "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                "social_insurance_participating, housing_fund_participating, "
                "resident_employee, execution_attribution_id, created_at) "
                "SELECT :successor_id, org_id, employee_id, id, '2026-07-01', '2026-07-31', "
                "expense_role, social_insurance_base_fen + 1, housing_fund_base_fen, "
                "social_insurance_participating, housing_fund_participating, resident_employee, "
                "current_setting('finance.execution_attribution_id')::uuid, "
                "now() "
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

    assert isinstance(postgres_engine, _Runtime)
    with _session(postgres_engine) as session:
        organization, _batch, _line, evidence, _event = confirmed_payroll(
            session,
            postgres_engine.org_id,
            postgres_engine.evidence_id,
            postgres_engine.authority,
            key="r6-evidence",
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
                "storage_path, metadata, execution_attribution_id, created_at) "
                "VALUES (:id, :org_id, :sha256, 'bad.txt', 'text/plain', 'r6', 1, "
                "'/r6/bad', '{}'::json, "
                "current_setting('finance.execution_attribution_id')::uuid, now())"
            ),
            {"id": uuid.uuid4(), "org_id": values["org_id"], "sha256": invalid_hash},
            "ck_evidence_sha256" if len(invalid_hash) == 64 else None,
        )

    with _session(postgres_engine) as session:
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
        session.commit()

    with _session(postgres_engine) as session:
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


def test_r6_005_cross_period_statutory_components_commit_with_scoped_sources(
    isolated_postgres_engine: object,
) -> None:
    """Two payroll periods compose when each payment component owns one source."""

    assert isinstance(isolated_postgres_engine, _Runtime)
    with _session(isolated_postgres_engine) as session:
        organization = _organization(session, isolated_postgres_engine)
        prepare_authenticated_bank_account(
            session,
            organization,
            authority=isolated_postgres_engine.authority,
            evidence_id=isolated_postgres_engine.evidence_id,
        )
        prepare_authenticated_bank_account(
            session,
            organization,
            booking_date=date(2026, 4, 5),
            authority=isolated_postgres_engine.authority,
            evidence_id=isolated_postgres_engine.evidence_id,
        )
        employee_id = register_payroll_facts(session, organization)
        _september_preview, september_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            evidence_id=isolated_postgres_engine.evidence_id,
            authority=isolated_postgres_engine.authority,
            key="r6-period-september",
        )
        _october_preview, october_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-04",
            evidence_id=isolated_postgres_engine.evidence_id,
            authority=isolated_postgres_engine.authority,
            key="r6-period-october",
        )
        source_items = [september_tax, october_tax]
        amounts = [item.original_amount_fen - item.settled_amount_fen for item in source_items]
        banks = [
            import_test_bank_transaction(
                session,
                organization,
                amount_fen=-amount,
                key=f"r6-cross-period-bank-{index}",
                booking_date=date(2026, 4, 6),
            )
            for index, amount in enumerate(amounts)
        ]
        source_event = session.get(BusinessEvent, september_tax.source_event_id)
        assert source_event is not None and source_event.evidence
        request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "r6-cross-period-components",
                "posting_date": "2026-04-06",
                "evidence_references": [source_event.evidence[0].id],
                "components": [
                    {
                        "key": f"tax-{index}",
                        "kind": "payable_settlement",
                        "business_date": "2026-04-06",
                        "payment_date": "2026-04-06",
                        "allocations": [{"open_item_id": item.id, "amount_fen": amount}],
                        "metadata": {"counterparty": {"id": item.counterparty_id}},
                    }
                    for index, (item, amount) in enumerate(zip(source_items, amounts, strict=True))
                ],
                "funds": [
                    {
                        "key": f"bank-{index}",
                        "account_code": "1002",
                        "direction": "payment",
                        "payment_date": "2026-04-06",
                        "amount_fen": amount,
                        "allocations": [{"component_key": f"tax-{index}", "amount_fen": amount}],
                        "bank_transaction_references": [{"id": bank.id}],
                    }
                    for index, (bank, amount) in enumerate(zip(banks, amounts, strict=True))
                ],
            }
        )
        result = FinanceService(session).record_event(request)
        assert result.status == "posted", result.errors
        session.commit()
        assert all(item.status == "settled" for item in source_items)
