"""R7 PostgreSQL commit-boundary coverage for cumulative payroll closures."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database
from conftest import (
    bind_authenticated_bank_account,
    import_test_bank_transaction,
    prepare_authenticated_bank_account,
)
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_payroll_service import payment_request, payroll_parameters, register_payroll_facts
from test_round5_provenance_postgres import _confirm as _genuine_confirm
from test_round5_provenance_postgres import (
    _post_full_salary_payment as _genuine_post_full_salary_payment,
)
from test_round5_provenance_postgres import (
    _post_regular_tax_source as _genuine_post_regular_tax_source,
)
from test_round5_provenance_postgres import _preview_regular as _genuine_preview_regular
from test_round5_provenance_postgres import (
    _preview_separate_bonus as _genuine_preview_separate_bonus,
)
from test_round6_integrity_postgres import _organization, _Runtime, _session

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import (
    BusinessEvent,
    EmployeePayrollProfileVersion,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
)
from ai_accounting.schemas import (
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService

pytestmark = pytest.mark.postgres


@pytest.fixture
def postgres_engine() -> object:
    with authenticated_business_database("finance_company", name="R7 完整性测试") as values:
        engine, org_id, evidence_id, authority = values
        yield _Runtime(engine, org_id, evidence_id, authority)


def _prepare_bank(
    session: Session,
    organization: Organization,
    runtime: object,
    *,
    booking_date: date = date(2026, 3, 5),
):
    assert isinstance(runtime, _Runtime)
    result = prepare_authenticated_bank_account(
        session,
        organization,
        booking_date=booking_date,
        authority=runtime.authority,
        evidence_id=runtime.evidence_id,
    )
    _restore_outer_attribution(session)
    return result


def _runtime_for(session: Session) -> _Runtime:
    runtime = session.info.get("test_postgres_runtime")
    assert isinstance(runtime, _Runtime)
    return runtime


def _restore_outer_attribution(session: Session) -> None:
    attribution_id = session.info.get("finance_execution_attribution_id")
    assert isinstance(attribution_id, uuid.UUID)
    session.execute(
        sa.text("SELECT set_config('finance.execution_attribution_id', :value, true)"),
        {"value": str(attribution_id)},
    )


def _preview_regular(session: Session, **kwargs: object) -> object:
    runtime = _runtime_for(session)
    result = _genuine_preview_regular(
        session,
        evidence_id=runtime.evidence_id,
        authority=runtime.authority,
        **kwargs,
    )
    _restore_outer_attribution(session)
    return result


def _preview_separate_bonus(session: Session, **kwargs: object) -> object:
    runtime = _runtime_for(session)
    result = _genuine_preview_separate_bonus(
        session,
        evidence_id=runtime.evidence_id,
        authority=runtime.authority,
        **kwargs,
    )
    _restore_outer_attribution(session)
    return result


def _confirm(session: Session, **kwargs: object) -> object:
    runtime = _runtime_for(session)
    result = _genuine_confirm(session, authority=runtime.authority, **kwargs)
    _restore_outer_attribution(session)
    return result


def _post_full_salary_payment(
    session: Session, organization: Organization, **kwargs: object
) -> tuple[object, OpenItem]:
    runtime = _runtime_for(session)
    result = _genuine_post_full_salary_payment(
        session,
        organization,
        evidence_id=runtime.evidence_id,
        authority=runtime.authority,
        **kwargs,
    )
    _restore_outer_attribution(session)
    return result


def _post_regular_tax_source(
    session: Session, organization: Organization, **kwargs: object
) -> tuple[object, OpenItem]:
    runtime = _runtime_for(session)
    result = _genuine_post_regular_tax_source(
        session,
        organization,
        evidence_id=runtime.evidence_id,
        authority=runtime.authority,
        **kwargs,
    )
    _restore_outer_attribution(session)
    return result


def _reverse_accrual(
    session: Session,
    *,
    org_id: uuid.UUID,
    batch_id: uuid.UUID,
    key: str,
    posting_date: date = date(2026, 4, 20),
) -> object:
    """Use the canonical typed accrual reversal path."""

    batch = session.get(PayrollBatch, batch_id)
    assert batch is not None and batch.business_event_id is not None
    result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=org_id,
            event_id=batch.business_event_id,
            idempotency_key=f"{key}-accrual-reversal",
            reason="R7 累计闭包规范冲正工资计提",
            posting_date=posting_date,
        )
    )
    return result


def _preview_annual_bonus(
    session: Session,
    *,
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    payroll_period: str,
    payment_date: date,
    tax_method: str,
    key: str,
    regular_payroll_batch_id: uuid.UUID | None = None,
) -> object:
    item: dict[str, object] = {
        "employee_id": employee_id,
        "annual_bonus_fen": 100_000,
    }
    if regular_payroll_batch_id is not None:
        item["regular_payroll_batch_id"] = regular_payroll_batch_id
    result = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "batch_kind": "annual_bonus",
                "payroll_period": payroll_period,
                "posting_date": payment_date,
                "payment_date": payment_date,
                "tax_method": tax_method,
                "evidence_references": [_runtime_for(session).evidence_id],
                "employee_items": [item],
            }
        )
    )
    assert result.status == "calculated", result.errors
    return result


def _preview_regular_salary(
    session: Session,
    *,
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    payroll_period: str,
    tax_reported_salary_fen: int,
    key: str,
) -> object:
    payment_date = date.fromisoformat(f"{payroll_period}-05")
    result = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "batch_kind": "regular",
                "payroll_period": payroll_period,
                "posting_date": payment_date,
                "payment_date": payment_date,
                "evidence_references": [_runtime_for(session).evidence_id],
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": tax_reported_salary_fen,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )
    assert result.status == "calculated", result.errors
    return result


def _register_second_employee(session: Session, *, org_id: uuid.UUID, key: str) -> uuid.UUID:
    service = FinanceService(session)
    registered = service.register_employee(
        RegisterEmployeeRequest(
            org_id=org_id,
            employee_code=f"R7-{key}-E002",
            name=f"R7 {key} 员工二",
            employment_start_date=date(2026, 3, 1),
            tax_withholding_start_date=date(2026, 3, 1),
            status="active",
        )
    )
    assert registered["status"] == "registered", registered
    employee_id = uuid.UUID(registered["employee_id"])
    profile = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=org_id,
            employee_id=employee_id,
            effective_from=date(2026, 3, 1),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=1_000_000,
            resident_employee=True,
        )
    )
    assert profile["status"] == "registered", profile
    return employee_id


def _register_payroll_facts_with_initial_policy(
    session: Session,
    *,
    organization: Organization,
    employee_code: str,
    employment_start_date: date,
    policy_effective_from: date,
    policy_effective_to: date,
    policy_version: str,
    parameters: dict[str, object],
) -> uuid.UUID:
    """Register unposted payroll facts with an explicit initial policy period."""

    _prepare_bank(
        session,
        organization,
        _runtime_for(session),
        booking_date=employment_start_date.replace(day=5),
    )
    service = FinanceService(session)
    employee = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code=employee_code,
            name=f"R7 {employee_code} 员工",
            employment_start_date=employment_start_date,
            tax_withholding_start_date=employment_start_date,
            status="active",
        )
    )
    assert employee["status"] == "registered", employee
    employee_id = uuid.UUID(employee["employee_id"])
    profile = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=employment_start_date,
            expense_role="payroll_management_expense",
            social_insurance_base_fen=1_000_000,
            housing_fund_base_fen=1_000_000,
            resident_employee=True,
        )
    )
    assert profile["status"] == "registered", profile
    policy = service.register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest(
            org_id=organization.id,
            region="测试地区",
            effective_from=policy_effective_from,
            effective_to=policy_effective_to,
            version=policy_version,
            source_url=(
                "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                "n3359382/201812/c4182700/content.html"
            ),
            parameters=parameters,
        )
    )
    assert policy["status"] == "registered", policy
    return employee_id


def _profile_successor_statement(*, attributed: bool = False) -> sa.TextClause:
    del attributed
    return sa.text(
        "INSERT INTO employee_payroll_profile_versions "
        "(id, org_id, employee_id, supersedes_id, effective_from, effective_to, "
        "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
        "social_insurance_participating, housing_fund_participating, resident_employee, "
        "created_at, execution_attribution_id) VALUES "
        "(:id, :org_id, :employee_id, :profile_id, :effective_from, :effective_to, "
        "'payroll_management_expense', 1000001, 1000001, TRUE, TRUE, TRUE, now(), "
        "current_setting('finance.execution_attribution_id')::uuid)"
    )


def _policy_successor_statement(*, attributed: bool = False) -> sa.TextClause:
    del attributed
    return sa.text(
        "INSERT INTO payroll_policy_versions "
        "(id, org_id, region, supersedes_id, effective_from, effective_to, version, "
        "source_url, parameters, created_at, execution_attribution_id) "
        "SELECT :id, :org_id, region, :policy_id, :effective_from, :effective_to, "
        ":version, source_url, parameters, now(), "
        "current_setting('finance.execution_attribution_id')::uuid "
        "FROM payroll_policy_versions WHERE id = :policy_id"
    )


def _version_ids(
    session: Session, *, batch_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    batch = session.get(PayrollBatch, batch_id)
    line = session.scalar(select(PayrollLine).where(PayrollLine.payroll_batch_id == batch_id))
    assert batch is not None and line is not None
    return batch.id, line.employee_payroll_profile_version_id, batch.policy_version_id


def _regular_statutory_sources(
    session: Session,
    *,
    organization: Organization,
    employee_id: uuid.UUID,
    payroll_period: str,
    key: str,
) -> tuple[object, dict[str, list[OpenItem]]]:
    payment_date = date.fromisoformat(f"{payroll_period}-05")
    _prepare_bank(session, organization, _runtime_for(session), booking_date=payment_date)
    preview = _preview_regular(
        session,
        org_id=organization.id,
        employee_id=employee_id,
        payroll_period=payroll_period,
        key=f"{key}-preview",
    )
    confirmed = _confirm(
        session,
        org_id=organization.id,
        preview=preview,
        key=f"{key}-confirm",
    )
    batch = session.get(PayrollBatch, preview.batch_id)
    line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
    )
    salary = session.scalar(
        select(OpenItem).where(
            OpenItem.org_id == organization.id,
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "salary",
        )
    )
    assert batch is not None and line is not None and salary is not None
    bank = import_test_bank_transaction(
        session,
        organization,
        amount_fen=-line.net_salary_fen,
        key=f"{key}-salary-bank",
        booking_date=payment_date,
    )
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
        key=f"{key}-salary",
    )
    request = request.model_copy(
        update={
            "posting_date": payment_date,
            "components": [
                request.components[0].model_copy(
                    update={
                        "business_date": payment_date,
                        "payment_date": payment_date,
                    }
                )
            ],
            "funds": [request.funds[0].model_copy(update={"payment_date": payment_date})],
        }
    )
    salary_payment = FinanceService(session).record_event(request)
    assert salary_payment.status == "posted", salary_payment.errors
    assert batch.business_event_id is not None
    employer_items = {
        item.payable_category: item
        for item in session.scalars(
            select(OpenItem).where(
                OpenItem.org_id == organization.id,
                OpenItem.source_event_id == batch.business_event_id,
                OpenItem.payable_category.in_(("employer_social", "employer_housing")),
            )
        ).all()
    }
    withheld_items = {
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
    assert set(employer_items) == {"employer_social", "employer_housing"}
    assert set(withheld_items) == {
        "withheld_employee_social",
        "withheld_employee_housing",
    }
    return preview, {
        "social_insurance": [
            employer_items["employer_social"],
            withheld_items["withheld_employee_social"],
        ],
        "housing_fund": [
            employer_items["employer_housing"],
            withheld_items["withheld_employee_housing"],
        ],
    }


def _stage_direct_statutory_payment(
    session: Session,
    *,
    organization: Organization,
    source_items: list[OpenItem],
    category: str,
    key: str,
) -> BusinessEvent:
    _prepare_bank(
        session,
        organization,
        _runtime_for(session),
        booking_date=date(2026, 7, 6),
    )
    del category
    amounts = [item.original_amount_fen - item.settled_amount_fen for item in source_items]
    assert all(amount > 0 for amount in amounts)
    banks = [
        import_test_bank_transaction(
            session,
            organization,
            amount_fen=-amount,
            key=f"{key}-bank-{index}",
            booking_date=date(2026, 7, 6),
        )
        for index, amount in enumerate(amounts)
    ]
    runtime = _runtime_for(session)
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": "2026-07-06",
            "evidence_references": [runtime.evidence_id],
            "components": [
                {
                    "key": f"statutory-{index}",
                    "kind": "payable_settlement",
                    "business_date": "2026-07-06",
                    "payment_date": "2026-07-06",
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
                    "payment_date": "2026-07-06",
                    "amount_fen": amount,
                    "allocations": [{"component_key": f"statutory-{index}", "amount_fen": amount}],
                    "bank_transaction_references": [{"id": bank.id}],
                }
                for index, (bank, amount) in enumerate(zip(banks, amounts, strict=True))
            ],
        }
    )
    result = FinanceService(session).record_event(request)
    assert result.status == "posted", result.errors
    event = session.get(BusinessEvent, result.event_id)
    assert event is not None
    return event


def test_r7_001_reversed_direct_batch_keeps_cumulative_downstream_blocked_at_commit(
    postgres_engine: object,
) -> None:
    """September reversal cannot free a successor while October stays final."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        september_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r7-september-preview",
        )
        september_confirmed = _confirm(
            session,
            org_id=organization.id,
            preview=september_preview,
            key="r7-september-confirm",
        )
        assert september_confirmed.status == "posted", september_confirmed.errors
        october_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-04",
            key="r7-october-preview",
        )
        october_confirmed = _confirm(
            session,
            org_id=organization.id,
            preview=october_preview,
            key="r7-october-confirm",
        )
        assert october_confirmed.status == "posted", october_confirmed.errors
        september = session.get(PayrollBatch, september_preview.batch_id)
        october = session.get(PayrollBatch, october_preview.batch_id)
        september_line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == september_preview.batch_id)
        )
        assert september is not None and october is not None and september_line is not None
        profile = session.get(
            EmployeePayrollProfileVersion,
            september_line.employee_payroll_profile_version_id,
        )
        policy = session.get(PayrollPolicyVersion, september.policy_version_id)
        assert profile is not None and policy is not None
        ids = {
            "org_id": organization.id,
            "employee_id": employee_id,
            "september_id": september.id,
            "october_id": october.id,
            "profile_id": profile.id,
            "policy_id": policy.id,
            "effective_from": date(2026, 3, 1),
            "effective_to": date(2026, 3, 31),
        }
        blocked = _reverse_accrual(
            session,
            org_id=organization.id,
            batch_id=september.id,
            key="r7-september",
        )
        assert blocked.status == "rejected"
        assert blocked.errors == ["REVERSE_DEPENDENT_PAYROLL_BATCHES_FIRST"]
        session.commit()

    for statement, code in (
        (
            _profile_successor_statement(),
            "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED",
        ),
        (
            _policy_successor_statement(),
            "R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED",
        ),
    ):
        with _session(postgres_engine) as session:
            october = session.get(PayrollBatch, ids["october_id"])
            assert october is not None and october.status == "posted"
            with pytest.raises(DBAPIError, match=code):
                session.execute(
                    statement,
                    {**ids, "id": uuid.uuid4(), "version": "r7-downstream-policy"},
                )
                session.commit()
            session.rollback()

    with _session(postgres_engine) as session:
        october_reversal = _reverse_accrual(
            session,
            org_id=ids["org_id"],
            batch_id=ids["october_id"],
            key="r7-october",
        )
        assert october_reversal.status == "posted", october_reversal.errors
        session.commit()

    with _session(postgres_engine) as session:
        september_reversal = _reverse_accrual(
            session,
            org_id=ids["org_id"],
            batch_id=ids["september_id"],
            key="r7-september-after-october",
        )
        assert september_reversal.status == "posted", september_reversal.errors
        session.commit()

    with _session(postgres_engine) as session:
        session.execute(
            _profile_successor_statement(),
            {**ids, "id": uuid.uuid4()},
        )
        session.execute(
            _policy_successor_statement(),
            {**ids, "id": uuid.uuid4(), "version": "r7-downstream-policy"},
        )
        session.commit()

    # The historical reversed predecessors must protect old consumers, but
    # cannot prohibit a new calculation that uses the corrected sources.
    with _session(postgres_engine) as session:
        rebuilt = _preview_regular(
            session,
            org_id=ids["org_id"],
            employee_id=ids["employee_id"],
            payroll_period="2026-03",
            key="r7-corrected-source-new-calculation",
        )
        confirmed = _confirm(
            session,
            org_id=ids["org_id"],
            preview=rebuilt,
            key="r7-corrected-source-new-confirm",
        )
        assert confirmed.status == "posted", confirmed.errors
        session.commit()


def test_r7_007_combined_enters_cumulative_closure(
    postgres_engine: object,
) -> None:
    """A same-month combined bonus stays in a reversed direct batch's closure."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        separate_preview = _preview_annual_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            payment_date=date(2026, 3, 4),
            tax_method="separate",
            key="r7-bonus-direct-separate-preview",
        )
        separate = _confirm(
            session,
            org_id=organization.id,
            preview=separate_preview,
            key="r7-bonus-direct-separate-confirm",
        )
        regular_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r7-bonus-regular-preview",
        )
        regular = _confirm(
            session,
            org_id=organization.id,
            preview=regular_preview,
            key="r7-bonus-regular-confirm",
        )
        combined_preview = _preview_annual_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            payment_date=date(2026, 3, 6),
            tax_method="combined",
            regular_payroll_batch_id=regular_preview.batch_id,
            key="r7-bonus-combined-preview",
        )
        combined = _confirm(
            session,
            org_id=organization.id,
            preview=combined_preview,
            key="r7-bonus-combined-confirm",
        )
        assert regular.event_id and combined.event_id and separate.event_id
        _batch_id, _profile_id, policy_id = _version_ids(
            session, batch_id=separate_preview.batch_id
        )
        ids = {
            "org_id": organization.id,
            "policy_id": policy_id,
            "effective_from": date(2026, 3, 4),
            "effective_to": date(2026, 3, 4),
            "version": "r7-combined-closure-policy",
        }
        _reverse_accrual(
            session,
            org_id=organization.id,
            batch_id=separate_preview.batch_id,
            key="r7-bonus-direct-separate",
        )
        session.commit()

    with _session(postgres_engine) as session:
        with pytest.raises(DBAPIError, match="R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED"):
            session.execute(_policy_successor_statement(), {**ids, "id": uuid.uuid4()})
            session.commit()
        session.rollback()

    with _session(postgres_engine) as session:
        combined_reversal = _reverse_accrual(
            session,
            org_id=ids["org_id"],
            batch_id=combined_preview.batch_id,
            key="r7-bonus-combined",
        )
        assert combined_reversal.status == "posted", combined_reversal.errors
        regular_reversal = _reverse_accrual(
            session,
            org_id=ids["org_id"],
            batch_id=regular_preview.batch_id,
            key="r7-bonus-regular",
        )
        assert regular_reversal.status == "posted", regular_reversal.errors
        session.commit()

    with _session(postgres_engine) as session:
        session.execute(_policy_successor_statement(), {**ids, "id": uuid.uuid4()})
        session.commit()


def test_r7_007_later_separate_bonus_does_not_enter_cumulative_closure(
    postgres_engine: object,
) -> None:
    """A separate bonus after a reversed regular batch remains posted and does not block."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        regular_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r7-later-separate-regular-preview",
        )
        regular = _confirm(
            session,
            org_id=organization.id,
            preview=regular_preview,
            key="r7-later-separate-regular-confirm",
        )
        separate_preview = _preview_annual_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-04",
            payment_date=date(2026, 4, 5),
            tax_method="separate",
            key="r7-later-separate-preview",
        )
        separate = _confirm(
            session,
            org_id=organization.id,
            preview=separate_preview,
            key="r7-later-separate-confirm",
        )
        assert regular.status == separate.status == "posted"
        _batch_id, _profile_id, policy_id = _version_ids(session, batch_id=regular_preview.batch_id)
        ids = {
            "org_id": organization.id,
            "policy_id": policy_id,
            "effective_from": date(2026, 3, 5),
            "effective_to": date(2026, 3, 5),
            "version": "r7-later-separate-policy",
        }
        _reverse_accrual(
            session,
            org_id=organization.id,
            batch_id=regular_preview.batch_id,
            key="r7-later-separate-regular",
        )
        session.commit()

    with _session(postgres_engine) as session:
        separate_batch = session.get(PayrollBatch, separate_preview.batch_id)
        assert separate_batch is not None and separate_batch.status == "posted"
        session.execute(_policy_successor_statement(), {**ids, "id": uuid.uuid4()})
        session.commit()


def test_r7_007_direct_separate_bonus_still_blocks_profile_and_policy(
    postgres_engine: object,
) -> None:
    """Separate tax is outside later closure only; a directly affected bonus blocks."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        preview = _preview_annual_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-03",
            payment_date=date(2026, 3, 6),
            tax_method="separate",
            key="r7-direct-separate-preview",
        )
        confirmed = _confirm(
            session,
            org_id=organization.id,
            preview=preview,
            key="r7-direct-separate-confirm",
        )
        assert confirmed.status == "posted"
        _batch_id, profile_id, policy_id = _version_ids(session, batch_id=preview.batch_id)
        base_ids = {
            "org_id": organization.id,
            "employee_id": employee_id,
            "profile_id": profile_id,
            "policy_id": policy_id,
        }
        session.commit()

    cases = (
        (
            _profile_successor_statement(),
            {
                **base_ids,
                "effective_from": date(2026, 3, 31),
                "effective_to": date(2026, 3, 31),
            },
            "R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED",
        ),
        (
            _policy_successor_statement(),
            {
                **base_ids,
                "effective_from": date(2026, 3, 6),
                "effective_to": date(2026, 3, 6),
                "version": "r7-direct-separate-policy",
            },
            "R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED",
        ),
    )
    for statement, values, code in cases:
        with _session(postgres_engine) as session:
            with pytest.raises(DBAPIError, match=code):
                session.execute(statement, {**values, "id": uuid.uuid4()})
                session.commit()
            session.rollback()


def test_r7_007_december_closure_does_not_cross_into_next_payment_tax_year(
    postgres_engine: object,
) -> None:
    """A new tax year releases the old policy while a continuing profile stays final."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        parameters = deepcopy(payroll_parameters())
        income_tax = parameters["income_tax"]
        assert isinstance(income_tax, dict)
        income_tax.update(
            {
                "version": "r7-income-tax-2025",
                "effective_from": "2025-01-01",
                "effective_to": "2025-12-31",
            }
        )
        employee_id = _register_payroll_facts_with_initial_policy(
            session,
            organization=organization,
            employee_code="R7-TAX-YEAR-E001",
            employment_start_date=date(2025, 12, 1),
            policy_effective_from=date(2025, 1, 1),
            policy_effective_to=date(2025, 12, 31),
            policy_version="r7-tax-year-2025",
            parameters=parameters,
        )
        registered = FinanceService(session).register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest(
                org_id=organization.id,
                region="测试地区",
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 12, 31),
                version="r7-tax-year-2026",
                source_url=(
                    "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                    "n3359382/201812/c4182700/content.html"
                ),
                parameters=payroll_parameters(),
            )
        )
        assert registered["status"] == "registered", registered
        december_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2025-12",
            key="r7-tax-year-december-preview",
        )
        december = _confirm(
            session,
            org_id=organization.id,
            preview=december_preview,
            key="r7-tax-year-december-confirm",
        )
        _prepare_bank(
            session,
            organization,
            postgres_engine,
            booking_date=date(2026, 1, 5),
        )
        january_preview = _preview_regular(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            payroll_period="2026-01",
            key="r7-tax-year-january-preview",
        )
        january = _confirm(
            session,
            org_id=organization.id,
            preview=january_preview,
            key="r7-tax-year-january-confirm",
        )
        assert december.status == january.status == "posted"
        _batch_id, profile_id, policy_id = _version_ids(session, batch_id=december_preview.batch_id)
        ids = {
            "org_id": organization.id,
            "employee_id": employee_id,
            "profile_id": profile_id,
            "policy_id": policy_id,
            "effective_from": date(2025, 12, 5),
            "effective_to": date(2025, 12, 31),
        }
        december_reversal = _reverse_accrual(
            session,
            org_id=organization.id,
            batch_id=december_preview.batch_id,
            key="r7-tax-year-december",
            posting_date=date(2025, 12, 20),
        )
        assert december_reversal.status == "posted", december_reversal.errors
        session.commit()

    with _session(postgres_engine) as session:
        january_batch = session.get(PayrollBatch, january_preview.batch_id)
        assert january_batch is not None and january_batch.status == "posted"
        with pytest.raises(DBAPIError, match="R6_FINAL_PAYROLL_PROFILE_CORRECTION_BLOCKED"):
            session.execute(
                _profile_successor_statement(attributed=True),
                {**ids, "id": uuid.uuid4(), "effective_to": None},
            )
            session.commit()
        session.rollback()

    with _session(postgres_engine) as session:
        session.execute(
            _policy_successor_statement(attributed=True),
            {**ids, "id": uuid.uuid4(), "version": "r7-december-correction"},
        )
        session.commit()


def test_r7_007_shared_policy_waits_for_every_employee_chain_in_fixed_lock_order(
    postgres_engine: object,
) -> None:
    """One remaining employee closure blocks a shared policy successor."""

    with _session(postgres_engine) as session:
        lock_function = session.scalar(
            sa.text(
                "SELECT pg_get_functiondef("
                "'finance_lock_final_payroll_dependency_guards()'::regprocedure)"
            )
        )
        assert lock_function is not None
        assert "ORDER BY guard_kind, dimension_key" in lock_function
        organization = _organization(session, postgres_engine)
        first_employee_id = register_payroll_facts(session, organization)
        second_employee_id = _register_second_employee(
            session, org_id=organization.id, key="shared-policy"
        )
        chains: list[tuple[object, object]] = []
        for label, employee_id, direct_period, downstream_period in (
            ("first", first_employee_id, "2026-03", "2026-04"),
            ("second", second_employee_id, "2026-05", "2026-06"),
        ):
            salary = 1_000_000 if label == "first" else 1_100_000
            direct = _preview_regular_salary(
                session,
                org_id=organization.id,
                employee_id=employee_id,
                payroll_period=direct_period,
                tax_reported_salary_fen=salary,
                key=f"r7-shared-{label}-direct-preview",
            )
            _confirm(
                session,
                org_id=organization.id,
                preview=direct,
                key=f"r7-shared-{label}-direct-confirm",
            )
            downstream = _preview_regular_salary(
                session,
                org_id=organization.id,
                employee_id=employee_id,
                payroll_period=downstream_period,
                tax_reported_salary_fen=salary,
                key=f"r7-shared-{label}-downstream-preview",
            )
            _confirm(
                session,
                org_id=organization.id,
                preview=downstream,
                key=f"r7-shared-{label}-downstream-confirm",
            )
            chains.append((direct, downstream))
        _batch_id, _profile_id, policy_id = _version_ids(session, batch_id=chains[0][0].batch_id)
        ids = {
            "org_id": organization.id,
            "policy_id": policy_id,
            "effective_from": date(2026, 3, 1),
            "effective_to": date(2026, 5, 31),
            "version": "r7-shared-policy-correction",
        }
        for index, (direct, _downstream) in enumerate(chains, start=1):
            blocked = _reverse_accrual(
                session,
                org_id=organization.id,
                batch_id=direct.batch_id,
                key=f"r7-shared-{index}-direct",
                posting_date=date(2026, 6, 20),
            )
            assert blocked.status == "rejected"
            assert blocked.errors == ["REVERSE_DEPENDENT_PAYROLL_BATCHES_FIRST"]
        first_downstream_reversal = _reverse_accrual(
            session,
            org_id=organization.id,
            batch_id=chains[0][1].batch_id,
            key="r7-shared-first-downstream",
            posting_date=date(2026, 6, 20),
        )
        assert first_downstream_reversal.status == "posted"
        session.commit()

    with _session(postgres_engine) as session:
        with pytest.raises(DBAPIError, match="R6_FINAL_PAYROLL_POLICY_CORRECTION_BLOCKED"):
            session.execute(_policy_successor_statement(), {**ids, "id": uuid.uuid4()})
            session.commit()
        session.rollback()

    with _session(postgres_engine) as session:
        second_downstream_reversal = _reverse_accrual(
            session,
            org_id=ids["org_id"],
            batch_id=chains[1][1].batch_id,
            key="r7-shared-second-downstream",
            posting_date=date(2026, 6, 20),
        )
        assert second_downstream_reversal.status == "posted"
        for index, (direct, _downstream) in enumerate(chains, start=1):
            direct_reversal = _reverse_accrual(
                session,
                org_id=ids["org_id"],
                batch_id=direct.batch_id,
                key=f"r7-shared-{index}-direct-after-downstream",
                posting_date=date(2026, 6, 20),
            )
            assert direct_reversal.status == "posted", direct_reversal.errors
        session.commit()

    with _session(postgres_engine) as session:
        session.execute(_policy_successor_statement(), {**ids, "id": uuid.uuid4()})
        session.commit()


def test_r7_002_iit_components_keep_distinct_payment_tax_months_at_commit(
    postgres_engine: object,
) -> None:
    """Distinct tax months compose when each settlement component owns its source."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        _prepare_bank(session, organization, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        _regular_preview, regular_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r7-iit-regular",
        )
        _prepare_bank(session, organization, postgres_engine, booking_date=date(2026, 4, 5))
        bonus_preview = _preview_separate_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            key="r7-iit-bonus-preview",
            payment_date=date(2026, 4, 5),
        )
        bonus = _confirm(
            session,
            org_id=organization.id,
            preview=bonus_preview,
            key="r7-iit-bonus-confirm",
        )
        _bonus_salary, bonus_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=bonus_preview.batch_id,
            accrual_event_id=bonus.event_id,
            key="r7-iit-bonus-salary",
        )
        event = _stage_direct_statutory_payment(
            session,
            organization=organization,
            source_items=[regular_tax, bonus_tax],
            category="individual_income_tax",
            key="r7-iit-cross-tax-month",
        )
        session.commit()
        assert event.status == "posted"


def test_r7_007_iit_components_keep_distinct_policy_version_sources(
    postgres_engine: object,
) -> None:
    """Distinct policy sources compose through separately scoped components."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        authority = _prepare_bank(session, organization, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        regular_preview, regular_tax = _post_regular_tax_source(
            session,
            organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r7-iit-policy-regular",
        )
        bonus_preview = _preview_separate_bonus(
            session,
            org_id=organization.id,
            employee_id=employee_id,
            key="r7-iit-policy-bonus-preview",
        )
        regular_batch = session.get(PayrollBatch, regular_preview.batch_id)
        bonus_batch = session.get(PayrollBatch, bonus_preview.batch_id)
        assert regular_batch is not None and bonus_batch is not None
        bonus = _confirm(
            session,
            org_id=organization.id,
            preview=bonus_preview,
            key="r7-iit-policy-bonus-confirm",
        )
        alternate_policy = PayrollPolicyVersion(
            org_id=organization.id,
            region="R7 个税关联列独立地区",
            effective_from=date(2025, 7, 1),
            effective_to=date(2026, 6, 30),
            version="r7-iit-relational-policy",
            source_url=regular_batch.policy_snapshot["source_url"],
            parameters=deepcopy(
                session.get(PayrollPolicyVersion, regular_batch.policy_version_id).parameters
            ),
        )
        session.add(alternate_policy)
        session.flush()
        _bonus_salary, bonus_tax = _post_full_salary_payment(
            session,
            organization,
            batch_id=bonus_preview.batch_id,
            accrual_event_id=bonus.event_id,
            key="r7-iit-policy-bonus-salary",
        )
        organization_id = organization.id
        alternate_policy_id = alternate_policy.id
        regular_tax_id = regular_tax.id
        bonus_tax_id = bonus_tax.id
        session.commit()

    with _session(postgres_engine) as session:
        session.execute(
            sa.text("ALTER TABLE payroll_batches DISABLE TRIGGER immutable_posted_payroll_batch")
        )
        session.execute(
            sa.update(PayrollBatch)
            .where(PayrollBatch.id == bonus_preview.batch_id)
            .values(policy_version_id=alternate_policy_id)
        )
        session.commit()

    with _session(postgres_engine) as session:
        session.execute(
            sa.text("ALTER TABLE payroll_batches ENABLE TRIGGER immutable_posted_payroll_batch")
        )
        session.commit()

    with _session(postgres_engine) as session:
        bind_authenticated_bank_account(session, authority)
        regular_batch = session.get(PayrollBatch, regular_preview.batch_id)
        bonus_batch = session.get(PayrollBatch, bonus_preview.batch_id)
        assert regular_batch is not None and bonus_batch is not None
        assert regular_batch.policy_version_id != bonus_batch.policy_version_id
        assert (
            regular_batch.policy_snapshot["income_tax_policy"]["id"]
            == bonus_batch.policy_snapshot["income_tax_policy"]["id"]
        )
        regular_tax = session.get(OpenItem, regular_tax_id)
        bonus_tax = session.get(OpenItem, bonus_tax_id)
        organization = session.get(Organization, organization_id)
        assert regular_tax is not None and bonus_tax is not None and organization is not None
        event = _stage_direct_statutory_payment(
            session,
            organization=organization,
            source_items=[regular_tax, bonus_tax],
            category="individual_income_tax",
            key="r7-iit-relational-policy-mismatch",
        )
        session.commit()
        assert event.status == "posted"


def test_r7_007_social_and_housing_accept_same_contribution_policy_and_period(
    postgres_engine: object,
) -> None:
    """Employer and employee sources from one batch share both contribution keys."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        _prepare_bank(session, organization, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        preview, source_groups = _regular_statutory_sources(
            session,
            organization=organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key="r7-contribution-compatible",
        )
        batch = session.get(PayrollBatch, preview.batch_id)
        assert batch is not None
        assert batch.policy_snapshot["contribution_policy"]["id"]
        for category in ("social_insurance", "housing_fund"):
            event = _stage_direct_statutory_payment(
                session,
                organization=organization,
                source_items=source_groups[category],
                category=category,
                key=f"r7-contribution-compatible-{category}",
            )
            assert event.status == "posted"
        session.commit()


@pytest.mark.parametrize("category", ["social_insurance", "housing_fund"])
def test_r7_007_social_and_housing_compose_different_payroll_periods(
    postgres_engine: object,
    category: str,
) -> None:
    """Different payroll periods compose through separately scoped components."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        _prepare_bank(session, organization, postgres_engine)
        employee_id = register_payroll_facts(session, organization)
        september, september_sources = _regular_statutory_sources(
            session,
            organization=organization,
            employee_id=employee_id,
            payroll_period="2026-03",
            key=f"r7-{category}-period-september",
        )
        october, october_sources = _regular_statutory_sources(
            session,
            organization=organization,
            employee_id=employee_id,
            payroll_period="2026-04",
            key=f"r7-{category}-period-october",
        )
        september_batch = session.get(PayrollBatch, september.batch_id)
        october_batch = session.get(PayrollBatch, october.batch_id)
        assert september_batch is not None and october_batch is not None
        assert (
            september_batch.policy_snapshot["contribution_policy"]["id"]
            == october_batch.policy_snapshot["contribution_policy"]["id"]
        )
        assert september_batch.payroll_period != october_batch.payroll_period
        event = _stage_direct_statutory_payment(
            session,
            organization=organization,
            source_items=[september_sources[category][0], october_sources[category][0]],
            category=category,
            key=f"r7-{category}-different-periods",
        )
        session.commit()
        assert event.status == "posted"


@pytest.mark.parametrize("category", ["social_insurance", "housing_fund"])
def test_r7_007_social_and_housing_compose_different_contribution_policies(
    postgres_engine: object,
    category: str,
) -> None:
    """Different contribution policies compose through separately scoped components."""

    with _session(postgres_engine) as session:
        organization = _organization(session, postgres_engine)
        parameters = deepcopy(payroll_parameters())
        income_tax = parameters["income_tax"]
        assert isinstance(income_tax, dict)
        income_tax.update(
            {
                "version": f"r7-{category}-income-tax-2026-h1",
                "effective_from": "2026-01-01",
                "effective_to": "2026-06-30",
            }
        )
        employee_id = _register_payroll_facts_with_initial_policy(
            session,
            organization=organization,
            employee_code=f"R7-{category}-E001",
            employment_start_date=date(2026, 3, 1),
            policy_effective_from=date(2026, 1, 1),
            policy_effective_to=date(2026, 6, 30),
            policy_version=f"r7-{category}-2026-h1",
            parameters=parameters,
        )
        parameters = deepcopy(payroll_parameters())
        income_tax = parameters["income_tax"]
        assert isinstance(income_tax, dict)
        income_tax.update(
            {
                "version": f"r7-{category}-income-tax-2026-h2",
                "effective_from": "2026-07-01",
                "effective_to": "2026-12-31",
            }
        )
        registered = FinanceService(session).register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest(
                org_id=organization.id,
                region="测试地区",
                effective_from=date(2026, 7, 1),
                effective_to=date(2026, 12, 31),
                version=f"r7-{category}-2026-h2",
                source_url=(
                    "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                    "n3359382/201812/c4182700/content.html"
                ),
                parameters=parameters,
            )
        )
        assert registered["status"] == "registered", registered
        december, december_sources = _regular_statutory_sources(
            session,
            organization=organization,
            employee_id=employee_id,
            payroll_period="2026-06",
            key=f"r7-{category}-policy-december",
        )
        january, january_sources = _regular_statutory_sources(
            session,
            organization=organization,
            employee_id=employee_id,
            payroll_period="2026-07",
            key=f"r7-{category}-policy-january",
        )
        december_batch = session.get(PayrollBatch, december.batch_id)
        january_batch = session.get(PayrollBatch, january.batch_id)
        assert december_batch is not None and january_batch is not None
        assert (
            december_batch.policy_snapshot["contribution_policy"]["id"]
            != january_batch.policy_snapshot["contribution_policy"]["id"]
        )
        event = _stage_direct_statutory_payment(
            session,
            organization=organization,
            source_items=[december_sources[category][0], january_sources[category][0]],
            category=category,
            key=f"r7-{category}-different-contribution-policies",
        )
        session.commit()
        assert event.status == "posted"
