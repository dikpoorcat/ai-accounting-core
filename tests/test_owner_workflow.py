from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.models import AccountingPeriod, Evidence, Organization
from ai_accounting.owner_workflow import OwnerWorkflowService
from ai_accounting.owner_workflow_schemas import (
    ConfirmExternalObligationRequest,
    ConfirmHistoricalObligationCompletionRequest,
    ConfirmOrganizationEstablishmentRequest,
    ConfirmPeriodMaterialCompletenessRequest,
    ConfirmWorkforceReviewRequest,
    GetOwnerWorkflowRequest,
)
from ai_accounting.schemas import RegisterEmployeeRequest
from ai_accounting.service import FinanceService


def _generate_august_period(
    session: Session, organization: Organization
) -> AccountingPeriod:
    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="owner-workflow-period.txt",
        source="test",
        size_bytes=1,
        storage_path="tests/owner-workflow-period.txt",
    )
    session.add(evidence)
    session.flush()
    generated = AccountingPeriodService(
        session, current_date=date(2026, 9, 2)
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-08",
            idempotency_key="owner-workflow-period-2026-08",
            confirmation_note="生成负责人流程测试期间。",
            evidence_references=[evidence.id],
        )
    )
    assert generated.status == "posted"
    period = session.get(AccountingPeriod, generated.period_id)
    assert period is not None
    return period


def _step(result: dict[str, object], code: str) -> dict[str, object]:
    return next(item for item in result["steps"] if item["code"] == code)  # type: ignore[index]


def test_closed_period_backup_status_uses_catalog_session_in_split_database_mode() -> None:
    org_id = uuid.uuid4()
    close_id = uuid.uuid4()
    business_session = MagicMock()
    catalog_session = MagicMock()
    business_session.get.return_value = SimpleNamespace(id=close_id)
    catalog_session.scalar.return_value = SimpleNamespace(status="completed")
    service = OwnerWorkflowService(
        business_session,
        current_date=date(2026, 9, 2),
        catalog_session=catalog_session,
    )

    result = service._close_step(  # noqa: SLF001 - verifies the split-session boundary
        SimpleNamespace(id=org_id),
        SimpleNamespace(status="closed", close_id=close_id, id=uuid.uuid4()),
        {},
    )

    assert result["completion_state"] == "completed"
    catalog_session.scalar.assert_called_once()
    business_session.scalar.assert_not_called()


def test_material_snapshot_uses_exists_instead_of_distinct_over_json_columns() -> None:
    org_id = uuid.uuid4()
    event_id = uuid.uuid4()
    session = MagicMock()
    session.scalars.return_value = [
        SimpleNamespace(
            id=event_id,
            event_type="expense_cash",
            status="posted",
            posting_date=date(2026, 8, 1),
            reversed_by_event_id=None,
            facts={"amounts": {"gross_amount_fen": 100}},
        )
    ]
    session.execute.return_value.scalars.return_value = []
    service = OwnerWorkflowService(session)

    service._material_snapshot(  # noqa: SLF001 - PostgreSQL query regression boundary
        org_id,
        SimpleNamespace(
            id=uuid.uuid4(),
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            calendar_year=2026,
            calendar_month=8,
        ),
    )

    statement = session.execute.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "EXISTS (SELECT *" in compiled
    assert "SELECT DISTINCT" not in compiled


def test_typed_owner_confirmations_survive_new_service_and_stale_on_upstream_change(
    session: Session,
    organization: Organization,
) -> None:
    period = _generate_august_period(session, organization)
    workflow = OwnerWorkflowService(session, current_date=date(2026, 9, 2))
    gates = workflow.close_gate_snapshot(organization.id, period)
    initial_close = AccountingPeriodService(
        session, current_date=date(2026, 9, 2)
    ).preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=organization.id,
            period_id=period.id,
            closing_date=period.end_date,
        )
    )
    assert {
        "ACCOUNTING_PERIOD_WORKFORCE_REVIEW_CURRENT",
        "ACCOUNTING_PERIOD_NON_BANK_MATERIAL_COMPLETENESS_CURRENT",
    } <= set(initial_close.data["blocker_codes"])
    assert initial_close.data["calculation"]["owner_workflow_close_gates"][
        "snapshot_hash"
    ] == gates["snapshot_hash"]

    workforce = workflow.confirm_workforce_review(
        ConfirmWorkforceReviewRequest(
            org_id=organization.id,
            period_id=period.id,
            workforce_snapshot_hash=gates["gates"]["workforce_review"][
                "source_snapshot_hash"
            ],
            change_state="no_change",
            idempotency_key="owner-workflow-workforce-2026-08",
            confirmation_note="确认八月没有人员及工资档案变化。",
        )
    )
    material = workflow.confirm_period_material_completeness(
        ConfirmPeriodMaterialCompletenessRequest(
            org_id=organization.id,
            period_id=period.id,
            activity_snapshot_hash=gates["gates"]["non_bank_materials"][
                "source_snapshot_hash"
            ],
            idempotency_key="owner-workflow-materials-2026-08",
            confirmation_note="确认八月非银行材料已经全部提供。",
        )
    )
    assert workforce["status"] == "confirmed"
    assert material["status"] == "confirmed"
    refreshed_close = AccountingPeriodService(
        session, current_date=date(2026, 9, 2)
    ).preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=organization.id,
            period_id=period.id,
            closing_date=period.end_date,
        )
    )
    assert "ACCOUNTING_PERIOD_WORKFORCE_REVIEW_CURRENT" not in refreshed_close.data[
        "blocker_codes"
    ]
    assert "ACCOUNTING_PERIOD_NON_BANK_MATERIAL_COMPLETENESS_CURRENT" not in (
        refreshed_close.data["blocker_codes"]
    )
    session.flush()
    session.expire_all()

    reopened = OwnerWorkflowService(session, current_date=date(2026, 9, 2)).get(
        GetOwnerWorkflowRequest(org_id=organization.id, period_id=period.id)
    )
    assert [item["code"] for item in reopened["steps"]] == [
        "BANK_STATEMENTS",
        "WORKFORCE_AND_PAY_CHANGES",
        "SOCIAL_INSURANCE_AND_HOUSING_FUND",
        "INDIVIDUAL_INCOME_TAX_WITHHOLDING",
        "NON_BANK_MATERIALS",
        "PERIOD_CLOSE_APPROVAL",
        "PERIODIC_TAX_AND_FINANCIAL_REPORTING",
        "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT",
        "ANNUAL_BUSINESS_REPORT",
    ]
    assert _step(reopened, "WORKFORCE_AND_PAY_CHANGES")["completion_state"] == "completed"
    assert _step(reopened, "WORKFORCE_AND_PAY_CHANGES")["symbol"] == "✅"
    assert _step(reopened, "SOCIAL_INSURANCE_AND_HOUSING_FUND")[
        "completion_state"
    ] == "not_applicable"
    assert _step(reopened, "NON_BANK_MATERIALS")["completion_state"] == "completed"

    registered = FinanceService(session).register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="E-WORKFLOW-NEW",
            name="新增员工",
            employment_start_date=date(2026, 8, 20),
            tax_withholding_start_date=date(2026, 8, 20),
        )
    )
    assert registered["status"] == "registered"
    stale = OwnerWorkflowService(session, current_date=date(2026, 9, 2)).get(
        GetOwnerWorkflowRequest(org_id=organization.id, period_id=period.id)
    )
    workforce_step = _step(stale, "WORKFORCE_AND_PAY_CHANGES")
    assert workforce_step["completion_state"] == "stale"
    assert workforce_step["close_gate_satisfied"] is False
    assert stale["close_gates"]["gates"]["workforce_review"]["stale"] is True


def test_annual_obligations_remain_overdue_until_typed_confirmation(
    session: Session,
    organization: Organization,
) -> None:
    period = _generate_august_period(session, organization)
    workflow = OwnerWorkflowService(session, current_date=date(2026, 9, 2))
    establishment = workflow.confirm_organization_establishment(
        ConfirmOrganizationEstablishmentRequest(
            org_id=organization.id,
            establishment_date=date(2024, 6, 1),
            idempotency_key="owner-workflow-establishment",
            confirmation_note="负责人确认公司成立日期。",
        )
    )
    assert establishment["status"] == "confirmed"

    before = workflow.get(
        GetOwnerWorkflowRequest(org_id=organization.id, period_id=period.id)
    )
    annual_eit = _step(before, "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT")
    business_report = _step(before, "ANNUAL_BUSINESS_REPORT")
    assert annual_eit["attention_state"] == "overdue"
    assert annual_eit["symbol"] == "⏰"
    assert annual_eit["deadline"] == "2025-05-31"
    assert business_report["attention_state"] == "overdue"
    assert business_report["deadline"] == "2025-06-30"

    obligation = annual_eit["obligation"]
    confirmed = workflow.confirm_external_obligation(
        ConfirmExternalObligationRequest(
            org_id=organization.id,
            obligation_id=uuid.UUID(obligation["obligation_id"]),
            source_snapshot_hash=obligation["source_snapshot_hash"],
            completion_status="submitted",
            completion_date=date(2025, 5, 20),
            idempotency_key="owner-workflow-annual-eit-2024",
            confirmation_note="负责人确认已完成二〇二四年度企业所得税汇算。",
        )
    )
    assert confirmed["status"] == "confirmed"
    after = OwnerWorkflowService(session, current_date=date(2026, 9, 2)).get(
        GetOwnerWorkflowRequest(org_id=organization.id, period_id=period.id)
    )
    # The next unconfirmed reporting year remains visible; completion of one year cannot hide it.
    next_annual = _step(after, "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT")
    assert next_annual["attention_state"] == "overdue"
    assert next_annual["obligation"]["scope_identity"] == "2025"
    assert _step(after, "ANNUAL_BUSINESS_REPORT")["attention_state"] == "overdue"


def test_historical_obligation_cutoffs_persist_without_invented_completion_dates(
    session: Session,
    organization: Organization,
) -> None:
    period = _generate_august_period(session, organization)
    workflow = OwnerWorkflowService(session, current_date=date(2026, 9, 2))
    before = workflow.get(
        GetOwnerWorkflowRequest(org_id=organization.id, period_id=period.id)
    )
    candidates = {
        item["obligation_code"]: item
        for item in before["historical_obligation_completion_candidates"]
    }
    assert candidates["periodic_tax_reporting"]["completion_through_identity"] == (
        "2026-Q2"
    )
    assert candidates["annual_enterprise_income_tax"][
        "completion_through_identity"
    ] == "2025"
    assert candidates["annual_business_report"]["completion_through_identity"] == (
        "2025"
    )

    other_organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA7654321P",
        name="另一家隔离测试公司",
        accounting_period_control_enabled=False,
    )
    cross_company = workflow.confirm_historical_obligation_completion(
        ConfirmHistoricalObligationCompletionRequest(
            org_id=other_organization.id,
            obligation_code="periodic_tax_reporting",
            completion_through_identity=candidates["periodic_tax_reporting"][
                "completion_through_identity"
            ],
            source_snapshot_hash=candidates["periodic_tax_reporting"][
                "source_snapshot_hash"
            ],
            completion_date_status="not_established",
            idempotency_key="historical-obligation-cross-company",
            confirmation_note="跨公司哈希必须被拒绝。",
        )
    )
    assert cross_company["status"] == "rejected"
    assert cross_company["errors"] == [
        "HISTORICAL_OBLIGATION_COMPLETION_SNAPSHOT_STALE"
    ]

    for code, candidate in candidates.items():
        confirmed = workflow.confirm_historical_obligation_completion(
            ConfirmHistoricalObligationCompletionRequest(
                org_id=organization.id,
                obligation_code=code,
                completion_through_identity=candidate["completion_through_identity"],
                source_snapshot_hash=candidate["source_snapshot_hash"],
                completion_date_status="not_established",
                idempotency_key=f"historical-obligation-{code}",
                confirmation_note="负责人确认全部适用历史义务已完成，具体完成日期未建立。",
            )
        )
        assert confirmed["status"] == "confirmed"
        assert confirmed["completion_date_status"] == "not_established"

    session.flush()
    session.expire_all()
    reopened = OwnerWorkflowService(session, current_date=date(2026, 9, 2)).get(
        GetOwnerWorkflowRequest(org_id=organization.id, period_id=period.id)
    )
    annual_eit = _step(reopened, "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT")
    business_report = _step(reopened, "ANNUAL_BUSINESS_REPORT")
    assert annual_eit["completion_state"] == "completed"
    assert business_report["completion_state"] == "completed"
    assert annual_eit["completion_proof"][0] == {
        "kind": "historical_obligation_completion_confirmation",
        "confirmation_id": annual_eit["completion_proof"][0]["confirmation_id"],
        "obligation_code": "annual_enterprise_income_tax",
        "completion_through_identity": "2025",
        "completion_date_status": "not_established",
    }
    assert business_report["completion_proof"][0]["completion_date_status"] == (
        "not_established"
    )

    q2_obligation = workflow._obligation(  # noqa: SLF001 - cutoff boundary regression
        organization.id,
        "periodic_tax_reporting",
        "quarter",
        "2026-Q2",
        date(2026, 7, 15),
        {"rule_version": "test"},
        [],
    )
    q3_obligation = workflow._obligation(  # noqa: SLF001 - future boundary regression
        organization.id,
        "periodic_tax_reporting",
        "quarter",
        "2026-Q3",
        date(2026, 10, 26),
        {"rule_version": "test"},
        [],
    )
    assert workflow._current_obligation_confirmation(q2_obligation) is not None  # noqa: SLF001
    assert workflow._current_obligation_confirmation(q3_obligation) is None  # noqa: SLF001
