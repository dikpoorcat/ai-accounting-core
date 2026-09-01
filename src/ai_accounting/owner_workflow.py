from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from .accounting_periods import canonical_sha256, china_current_date
from .models import (
    AccountingPeriod,
    AccountingPeriodClose,
    AccountingPeriodCloseBackup,
    AuditLog,
    BusinessEvent,
    Employee,
    Evidence,
    ExternalObligationConfirmation,
    FinancialStatementOpeningBalanceConfirmation,
    Organization,
    OrganizationEstablishmentConfirmation,
    OwnerPeriodConfirmation,
    PayrollBatch,
    PayrollContributionActualUse,
    PayrollContributionAssessmentConfirmation,
    PayrollLine,
    PayrollTaxImportExport,
    event_evidence,
)
from .owner_workflow_schemas import (
    ConfirmExternalObligationRequest,
    ConfirmOrganizationEstablishmentRequest,
    ConfirmPayrollContributionAssessmentRequest,
    ConfirmPeriodMaterialCompletenessRequest,
    ConfirmWorkforceReviewRequest,
    GetOwnerWorkflowRequest,
    PreviewPayrollContributionAssessmentRequest,
)
from .payroll import (
    ContributionActualOverride,
    ContributionBaseKind,
    ContributionBases,
    YearMonth,
    apply_contribution_actuals,
    calculate_contributions,
)
from .payroll.types import CalculationValidationError, NeedsInformationError
from .service import FinanceService

OWNER_WORKFLOW_VERSION = "owner_monthly_workflow_cn_2026.6"
OWNER_WORKFLOW_CLOSE_GATE_VERSION = "owner_workflow_close_gates_2026.1"
OWNER_WORKFLOW_CLOSE_GATE_EFFECTIVE_FROM = date(2026, 8, 1)

_IIT_SOURCE_URL = (
    "https://www.chinatax.gov.cn/n810219/n810744/n3752930/"
    "n3752974/c3963396/content.html"
)
_SOCIAL_SOURCE_URL = (
    "https://zhejiang.chinatax.gov.cn/art/2023/8/10/art_13314_595952.html"
)
_TAX_DEADLINE_SOURCE_URL = (
    "https://fgk.chinatax.gov.cn/zcfgk/c102424/c5245729/content.html"
)
_BUSINESS_REPORT_SOURCE_URL = (
    "https://www.samr.gov.cn/cms_files/filemanager/samr/www/samrnew/"
    "samrgkml/nsjg/fgs/202203/W020220302478403609033.pdf"
)
_OBLIGATION_NAMESPACE = uuid.UUID("c9e9b190-a7c2-4fe2-b79c-a0635968289b")

# State Taxation Administration's published 2026 holiday-adjusted monthly deadlines.
_CN_2026_MONTHLY_FILING_DEADLINES = {
    1: date(2026, 1, 20),
    2: date(2026, 2, 24),
    3: date(2026, 3, 16),
    4: date(2026, 4, 20),
    5: date(2026, 5, 22),
    6: date(2026, 6, 15),
    7: date(2026, 7, 15),
    8: date(2026, 8, 17),
    9: date(2026, 9, 15),
    10: date(2026, 10, 26),
    11: date(2026, 11, 16),
    12: date(2026, 12, 15),
}

_STEPS = (
    (1, "BANK_STATEMENTS", "银行流水"),
    (2, "WORKFORCE_AND_PAY_CHANGES", "员工及工资变动"),
    (3, "SOCIAL_INSURANCE_AND_HOUSING_FUND", "社保及公积金"),
    (4, "INDIVIDUAL_INCOME_TAX_WITHHOLDING", "个人所得税"),
    (5, "NON_BANK_MATERIALS", "票据及非银行业务"),
    (6, "PERIOD_CLOSE_APPROVAL", "关账确认"),
    (7, "PERIODIC_TAX_AND_FINANCIAL_REPORTING", "税费申报及财务报表"),
    (8, "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT", "企业所得税年度汇算清缴"),
    (9, "ANNUAL_BUSINESS_REPORT", "工商年报"),
)


class OwnerWorkflowService:
    """Single source of truth for the owner's durable monthly workflow."""

    def __init__(
        self,
        session: Session,
        *,
        current_date: date | None = None,
        catalog_session: Session | None = None,
    ) -> None:
        self.session = session
        self.catalog_session = catalog_session
        self._current_date = current_date

    @property
    def today(self) -> date:
        return self._current_date or china_current_date()

    def get(self, request: GetOwnerWorkflowRequest) -> dict[str, Any]:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        period = self._resolve_period(request.org_id, request.period_id)
        if period is None:
            return {
                "status": "needs_information",
                "errors": [],
                "missing_information": [
                    {
                        "code": "OWNER_WORKFLOW_ACCOUNTING_PERIOD_REQUIRED",
                        "fields": ["period_id"],
                        "message": "an accounting period must be generated first",
                    }
                ],
            }

        gate_snapshot = self.close_gate_snapshot(request.org_id, period)
        steps = self._build_steps(organization, period, gate_snapshot)
        current_action = self._select_current_action(steps)
        for step in steps:
            step.pop("_ready", None)
        exports = self._period_exports(request.org_id, self._period_month(period))
        return {
            "status": "ok",
            "workflow_version": OWNER_WORKFLOW_VERSION,
            "generated_on": self.today.isoformat(),
            "organization": {"id": str(organization.id), "name": organization.name},
            "period": self._period_payload(period),
            "current_action": current_action,
            "steps": steps,
            "close_gates": gate_snapshot,
            "existing_exports": exports,
            "external_materials_completeness": (
                "owner_confirmed_current"
                if gate_snapshot["gates"]["non_bank_materials"]["satisfied"]
                else "not_established"
            ),
        }

    def preview_payroll_contribution_assessment(
        self, request: PreviewPayrollContributionAssessmentRequest
    ) -> dict[str, Any]:
        period = self._period_for_org(request.org_id, request.period_id)
        if period is None:
            return {"status": "rejected", "errors": ["ACCOUNTING_PERIOD_NOT_FOUND"]}
        snapshot = self._contribution_snapshot(request.org_id, period)
        if snapshot["missing_information"]:
            return {
                "status": "needs_information",
                "period_id": str(period.id),
                "contribution_period": self._period_month(period),
                "missing_information": snapshot["missing_information"],
            }
        return {
            "status": "calculated",
            "period_id": str(period.id),
            "contribution_period": self._period_month(period),
            "calculation_hash": snapshot["calculation_hash"],
            "calculation": snapshot["calculation"],
        }

    def confirm_workforce_review(
        self, request: ConfirmWorkforceReviewRequest
    ) -> dict[str, Any]:
        period = self._period_for_org(request.org_id, request.period_id)
        if period is None:
            return {"status": "rejected", "errors": ["ACCOUNTING_PERIOD_NOT_FOUND"]}
        snapshot = self._workforce_snapshot(request.org_id, period)
        if request.workforce_snapshot_hash != snapshot["hash"]:
            return {
                "status": "rejected",
                "errors": ["WORKFORCE_REVIEW_SNAPSHOT_STALE"],
                "current_snapshot_hash": snapshot["hash"],
            }
        return self._confirm_period_fact(
            request=request,
            period=period,
            fact_type="workforce_review",
            state=request.change_state,
            source_snapshot_hash=snapshot["hash"],
            source_snapshot=snapshot["data"],
            supersedes_id=request.supersedes_confirmation_id,
        )

    def confirm_period_material_completeness(
        self, request: ConfirmPeriodMaterialCompletenessRequest
    ) -> dict[str, Any]:
        period = self._period_for_org(request.org_id, request.period_id)
        if period is None:
            return {"status": "rejected", "errors": ["ACCOUNTING_PERIOD_NOT_FOUND"]}
        snapshot = self._material_snapshot(request.org_id, period)
        if request.activity_snapshot_hash != snapshot["hash"]:
            return {
                "status": "rejected",
                "errors": ["PERIOD_MATERIAL_SNAPSHOT_STALE"],
                "current_snapshot_hash": snapshot["hash"],
            }
        return self._confirm_period_fact(
            request=request,
            period=period,
            fact_type="non_bank_materials",
            state="complete",
            source_snapshot_hash=snapshot["hash"],
            source_snapshot=snapshot["data"],
            supersedes_id=request.supersedes_confirmation_id,
        )

    def confirm_payroll_contribution_assessment(
        self, request: ConfirmPayrollContributionAssessmentRequest
    ) -> dict[str, Any]:
        period = self._period_for_org(request.org_id, request.period_id)
        if period is None:
            return {"status": "rejected", "errors": ["ACCOUNTING_PERIOD_NOT_FOUND"]}
        preview = self._contribution_snapshot(request.org_id, period)
        if preview["missing_information"]:
            return {
                "status": "needs_information",
                "missing_information": preview["missing_information"],
            }
        if preview["calculation_hash"] != request.calculation_hash:
            return {
                "status": "rejected",
                "errors": ["PAYROLL_CONTRIBUTION_ASSESSMENT_SNAPSHOT_STALE"],
                "current_calculation_hash": preview["calculation_hash"],
            }
        payload_hash = self._request_hash(
            "finance_confirm_payroll_contribution_assessment", request
        )
        replay = self._idempotent_replay(
            PayrollContributionAssessmentConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "PAYROLL_CONTRIBUTION_ASSESSMENT_IDEMPOTENCY_PAYLOAD_MISMATCH",
        )
        if replay is not None:
            return replay
        active = self._active_contribution_confirmation(request.org_id, period.id)
        invalid = self._validate_supersedes(
            active,
            request.supersedes_confirmation_id,
            "PAYROLL_CONTRIBUTION_ASSESSMENT_CORRECTION_REQUIRES_SUPERSEDES",
            "INVALID_PAYROLL_CONTRIBUTION_ASSESSMENT_SUPERSEDES",
        )
        if invalid is not None:
            return invalid
        try:
            evidence = self._evidence_snapshot(request.org_id, request.evidence_references)
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        totals = preview["calculation"]["totals"]
        payment_status = {
            "declared_paid": "paid",
            "declared_unpaid": "unpaid",
            "not_declared": "not_applicable",
        }[request.declaration_status]
        row = PayrollContributionAssessmentConfirmation(
            org_id=request.org_id,
            period_id=period.id,
            contribution_period=self._period_month(period),
            declaration_status=request.declaration_status,
            declaration_date=request.declaration_date,
            payment_status=payment_status,
            payment_date=request.payment_date,
            external_reference=request.external_reference,
            calculation_hash=preview["calculation_hash"],
            calculation=preview["calculation"],
            employee_social_insurance_fen=totals["employee_social_insurance_fen"],
            employer_social_insurance_fen=totals["employer_social_insurance_fen"],
            employee_housing_fund_fen=totals["employee_housing_fund_fen"],
            employer_housing_fund_fen=totals["employer_housing_fund_fen"],
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            confirmation_note=request.confirmation_note,
            evidence_snapshot=evidence,
            supersedes_id=active.id if active is not None else None,
        )
        result = self._persist_row(
            row,
            PayrollContributionAssessmentConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "PAYROLL_CONTRIBUTION_ASSESSMENT_CONCURRENT_WRITE_CONFLICT",
        )
        if result is not None:
            return result
        self._audit(
            request.org_id,
            "payroll_contribution_assessment_confirmed",
            {"confirmation_id": str(row.id), "period_id": str(period.id)},
        )
        return {
            "status": "confirmed",
            "confirmation_id": str(row.id),
            "calculation_hash": row.calculation_hash,
            "verification_level": "evidence_attached" if evidence else "owner_confirmed",
        }

    def confirm_external_obligation(
        self, request: ConfirmExternalObligationRequest
    ) -> dict[str, Any]:
        organization = self.session.get(Organization, request.org_id)
        if organization is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        obligation = next(
            (
                item
                for item in self._obligation_catalog(organization)
                if item["obligation_id"] == str(request.obligation_id)
            ),
            None,
        )
        if obligation is None:
            return {"status": "rejected", "errors": ["EXTERNAL_OBLIGATION_NOT_FOUND"]}
        if obligation["source_snapshot_hash"] != request.source_snapshot_hash:
            return {
                "status": "rejected",
                "errors": ["EXTERNAL_OBLIGATION_SNAPSHOT_STALE"],
                "current_source_snapshot_hash": obligation["source_snapshot_hash"],
            }
        payload_hash = self._request_hash("finance_confirm_external_obligation", request)
        replay = self._idempotent_replay(
            ExternalObligationConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "EXTERNAL_OBLIGATION_IDEMPOTENCY_PAYLOAD_MISMATCH",
        )
        if replay is not None:
            return replay
        active = self._active_obligation_confirmation(request.org_id, request.obligation_id)
        invalid = self._validate_supersedes(
            active,
            request.supersedes_confirmation_id,
            "EXTERNAL_OBLIGATION_CORRECTION_REQUIRES_SUPERSEDES",
            "INVALID_EXTERNAL_OBLIGATION_SUPERSEDES",
        )
        if invalid is not None:
            return invalid
        try:
            evidence = self._evidence_snapshot(request.org_id, request.evidence_references)
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        row = ExternalObligationConfirmation(
            org_id=request.org_id,
            obligation_id=request.obligation_id,
            obligation_code=obligation["code"],
            obligation_scope=obligation["scope"],
            source_snapshot_hash=obligation["source_snapshot_hash"],
            completion_status=request.completion_status,
            completion_date=request.completion_date,
            external_reference=request.external_reference,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            confirmation_note=request.confirmation_note,
            evidence_snapshot=evidence,
            supersedes_id=active.id if active is not None else None,
        )
        result = self._persist_row(
            row,
            ExternalObligationConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "EXTERNAL_OBLIGATION_CONCURRENT_WRITE_CONFLICT",
        )
        if result is not None:
            return result
        self._audit(
            request.org_id,
            "external_obligation_confirmed",
            {"confirmation_id": str(row.id), "obligation_id": str(row.obligation_id)},
        )
        return {
            "status": "confirmed",
            "confirmation_id": str(row.id),
            "obligation_id": str(row.obligation_id),
            "verification_level": "evidence_attached" if evidence else "owner_confirmed",
        }

    def confirm_organization_establishment(
        self, request: ConfirmOrganizationEstablishmentRequest
    ) -> dict[str, Any]:
        if self.session.get(Organization, request.org_id) is None:
            return {"status": "rejected", "errors": ["ORGANIZATION_NOT_FOUND"]}
        payload_hash = self._request_hash(
            "finance_confirm_organization_establishment", request
        )
        replay = self._idempotent_replay(
            OrganizationEstablishmentConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "ORGANIZATION_ESTABLISHMENT_IDEMPOTENCY_PAYLOAD_MISMATCH",
        )
        if replay is not None:
            return replay
        active = self._active_establishment_confirmation(request.org_id)
        invalid = self._validate_supersedes(
            active,
            request.supersedes_confirmation_id,
            "ORGANIZATION_ESTABLISHMENT_CORRECTION_REQUIRES_SUPERSEDES",
            "INVALID_ORGANIZATION_ESTABLISHMENT_SUPERSEDES",
        )
        if invalid is not None:
            return invalid
        try:
            evidence = self._evidence_snapshot(request.org_id, request.evidence_references)
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        row = OrganizationEstablishmentConfirmation(
            org_id=request.org_id,
            establishment_date=request.establishment_date,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            confirmation_note=request.confirmation_note,
            evidence_snapshot=evidence,
            supersedes_id=active.id if active is not None else None,
        )
        result = self._persist_row(
            row,
            OrganizationEstablishmentConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "ORGANIZATION_ESTABLISHMENT_CONCURRENT_WRITE_CONFLICT",
        )
        if result is not None:
            return result
        return {
            "status": "confirmed",
            "confirmation_id": str(row.id),
            "establishment_date": row.establishment_date.isoformat(),
            "verification_level": "evidence_attached" if evidence else "owner_confirmed",
        }

    def close_gate_snapshot(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> dict[str, Any]:
        workforce = self._workforce_snapshot(org_id, period)
        workforce_fact = self._active_period_confirmation(
            org_id, period.id, "workforce_review"
        )
        workforce_current = bool(
            workforce_fact is not None
            and workforce_fact.source_snapshot_hash == workforce["hash"]
        )

        material = self._material_snapshot(org_id, period)
        material_fact = self._active_period_confirmation(
            org_id, period.id, "non_bank_materials"
        )
        material_current = bool(
            material_fact is not None and material_fact.source_snapshot_hash == material["hash"]
        )

        contribution = self._contribution_snapshot(org_id, period)
        assessment = self._active_contribution_confirmation(org_id, period.id)
        active_employee_count = contribution["active_employee_count"]
        contribution_current = active_employee_count == 0
        payroll_match: dict[str, Any] = {
            "satisfied": active_employee_count == 0,
            "batch_id": None,
            "calculation_hash": None,
            "reason": "not_applicable" if active_employee_count == 0 else "missing",
        }
        if active_employee_count and not contribution["missing_information"]:
            assessment_current = bool(
                assessment is not None
                and assessment.calculation_hash == contribution["calculation_hash"]
            )
            payroll_match = self._posted_payroll_matches_contribution(
                org_id, period, contribution["calculation"]
            )
            contribution_current = assessment_current and payroll_match["satisfied"]
        else:
            assessment_current = active_employee_count == 0

        return {
            "version": OWNER_WORKFLOW_CLOSE_GATE_VERSION,
            "effective_from": OWNER_WORKFLOW_CLOSE_GATE_EFFECTIVE_FROM.isoformat(),
            "enforced_for_period": period.start_date >= OWNER_WORKFLOW_CLOSE_GATE_EFFECTIVE_FROM,
            "snapshot_hash": canonical_sha256(
                {
                    "version": OWNER_WORKFLOW_CLOSE_GATE_VERSION,
                    "period_id": str(period.id),
                    "workforce_snapshot_hash": workforce["hash"],
                    "workforce_confirmation_id": (
                        str(workforce_fact.id) if workforce_current and workforce_fact else None
                    ),
                    "contribution_calculation_hash": contribution.get("calculation_hash"),
                    "contribution_confirmation_id": (
                        str(assessment.id) if assessment_current and assessment else None
                    ),
                    "payroll_batch_id": payroll_match["batch_id"],
                    "material_snapshot_hash": material["hash"],
                    "material_confirmation_id": (
                        str(material_fact.id) if material_current and material_fact else None
                    ),
                }
            ),
            "gates": {
                "workforce_review": {
                    "satisfied": workforce_current,
                    "source_snapshot_hash": workforce["hash"],
                    "confirmation_id": str(workforce_fact.id) if workforce_fact else None,
                    "stale": workforce_fact is not None and not workforce_current,
                },
                "contribution_accounting": {
                    "satisfied": contribution_current,
                    "active_employee_count": active_employee_count,
                    "calculation_hash": contribution.get("calculation_hash"),
                    "confirmation_id": str(assessment.id) if assessment else None,
                    "confirmation_current": assessment_current,
                    "declaration_status": (
                        assessment.declaration_status if assessment_current and assessment else None
                    ),
                    "payroll": payroll_match,
                    "missing_information": contribution["missing_information"],
                },
                "non_bank_materials": {
                    "satisfied": material_current,
                    "source_snapshot_hash": material["hash"],
                    "confirmation_id": str(material_fact.id) if material_fact else None,
                    "stale": material_fact is not None and not material_current,
                },
            },
        }

    def _build_steps(
        self,
        organization: Organization,
        period: AccountingPeriod,
        gates: dict[str, Any],
    ) -> list[dict[str, Any]]:
        builders = {
            "BANK_STATEMENTS": self._bank_step,
            "WORKFORCE_AND_PAY_CHANGES": self._workforce_step,
            "SOCIAL_INSURANCE_AND_HOUSING_FUND": self._contribution_step,
            "INDIVIDUAL_INCOME_TAX_WITHHOLDING": self._iit_step,
            "NON_BANK_MATERIALS": self._materials_step,
            "PERIOD_CLOSE_APPROVAL": self._close_step,
            "PERIODIC_TAX_AND_FINANCIAL_REPORTING": self._periodic_reporting_step,
            "ANNUAL_ENTERPRISE_INCOME_TAX_SETTLEMENT": self._annual_eit_step,
            "ANNUAL_BUSINESS_REPORT": self._annual_business_report_step,
        }
        result = []
        for order, code, label in _STEPS:
            step = builders[code](organization, period, gates)
            step.update({"order": order, "code": code, "label": label})
            step.setdefault("deadline", None)
            step.setdefault("completion_proof", [])
            step.setdefault("missing_facts", [])
            step.setdefault("next_owner_action", None)
            step.setdefault("close_gate_satisfied", True)
            step.setdefault("_ready", True)
            step["symbol"] = self._base_symbol(step)
            result.append(step)
        return result

    def _bank_step(
        self, organization: Organization, period: AccountingPeriod, _gates: dict[str, Any]
    ) -> dict[str, Any]:
        if period.status == "closed":
            return self._completed([{"kind": "period_closed", "period_id": str(period.id)}])
        from .accounting_period_service import AccountingPeriodService

        reconciliations, issues = AccountingPeriodService(
            self.session, current_date=self.today
        )._current_bank_reconciliations(organization.id, period)
        if not issues:
            return self._completed(
                [
                    {
                        "kind": "current_bank_reconciliation",
                        "reconciliation_ids": [str(item.id) for item in reconciliations],
                    }
                ]
            )
        return self._incomplete(
            missing=issues,
            action="请补齐并确认本期全部银行账户流水及对账。",
            close_gate=False,
        )

    def _workforce_step(
        self, _organization: Organization, _period: AccountingPeriod, gates: dict[str, Any]
    ) -> dict[str, Any]:
        gate = gates["gates"]["workforce_review"]
        if gate["satisfied"]:
            return self._completed(
                [
                    {
                        "kind": "owner_workforce_review",
                        "confirmation_id": gate["confirmation_id"],
                        "source_snapshot_hash": gate["source_snapshot_hash"],
                    }
                ]
            )
        return self._incomplete(
            state="stale" if gate["stale"] else "incomplete",
            missing=["workforce_review"],
            action="请确认本月是否有入离职、停薪、工资奖金、参保或基数变化。",
            close_gate=False,
        )

    def _contribution_step(
        self, _organization: Organization, _period: AccountingPeriod, gates: dict[str, Any]
    ) -> dict[str, Any]:
        gate = gates["gates"]["contribution_accounting"]
        if gate["active_employee_count"] == 0:
            return self._not_applicable("本期没有适用员工。")
        assessment = self._active_contribution_confirmation(
            _organization.id, _period.id
        )
        if gate["satisfied"]:
            proof = [
                {
                    "kind": "contribution_assessment",
                    "confirmation_id": gate["confirmation_id"],
                    "calculation_hash": gate["calculation_hash"],
                },
                {
                    "kind": "posted_regular_payroll",
                    "batch_id": gate["payroll"]["batch_id"],
                },
            ]
            if assessment is not None and assessment.declaration_status == "not_declared":
                return self._incomplete(
                    missing=["external_contribution_declaration"],
                    action="会计计提已完成；请完成社保及公积金外部申报并确认结果。",
                    close_gate=True,
                ) | {"completion_proof": proof}
            result = self._completed(proof)
            if assessment is not None and assessment.declaration_status == "declared_unpaid":
                deadline = self._next_month_deadline(_period.end_date)
                result.update(
                    {
                        "attention_state": (
                            "overdue" if self.today > deadline else "due"
                        ),
                        "deadline": deadline.isoformat(),
                        "next_owner_action": (
                            f"请在 {deadline.isoformat()} 前完成社保及公积金缴款。"
                        ),
                    }
                )
            return result
        if gate["missing_information"]:
            missing = [item["code"] for item in gate["missing_information"]]
            action = "请先补齐员工社保公积金档案或有效政策。"
        elif not gate["confirmation_current"]:
            missing = ["contribution_assessment_confirmation"]
            action = "请确认本期社保及公积金申报核定结果。"
        else:
            missing = ["posted_regular_payroll_using_same_assessment"]
            action = "核定金额已确认；请确认工资方案，内核将用同一核定快照完成计提。"
        return self._incomplete(missing=missing, action=action, close_gate=False)

    def _iit_step(
        self, organization: Organization, period: AccountingPeriod, gates: dict[str, Any]
    ) -> dict[str, Any]:
        contribution_gate = gates["gates"]["contribution_accounting"]
        if contribution_gate["active_employee_count"] and not contribution_gate["satisfied"]:
            return self._incomplete(
                state="waiting",
                attention="waiting_dependency",
                missing=["posted_regular_payroll_using_confirmed_contribution_assessment"],
                action=None,
                ready=False,
            )
        obligations = []
        for candidate in self.session.scalars(
            select(AccountingPeriod)
            .where(AccountingPeriod.org_id == organization.id)
            .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
        ):
            source = self._posted_payroll_source_snapshot(organization.id, candidate)
            if source["declared_line_count"]:
                obligations.append(self._iit_obligation(organization, candidate))
        if not obligations:
            return self._not_applicable("内核当前没有工资个税扣缴义务。")
        pending = [
            obligation
            for obligation in obligations
            if self._current_obligation_confirmation(obligation) is None
        ]
        if not pending:
            obligation = obligations[-1]
            confirmation = self._current_obligation_confirmation(obligation)
            assert confirmation is not None
            return self._completed(
                [
                    {
                        "kind": "external_obligation_confirmation",
                        "confirmation_id": str(confirmation.id),
                        "obligation_id": obligation["obligation_id"],
                    }
                ]
            )
        obligation = min(pending, key=lambda item: (item["deadline"], item["obligation_id"]))
        obligation_period = self._period_for_org(
            organization.id, uuid.UUID(obligation["source"]["period_id"])
        )
        assert obligation_period is not None
        exports = self._period_exports(
            organization.id, self._period_month(obligation_period)
        )
        current_export = next((item for item in exports if item["current"]), None)
        deadline = date.fromisoformat(obligation["deadline"])
        action = (
            "个税导入文件需要自动生成并交付桌面。"
            if current_export is None
            else "请在税务客户端导入核对并完成个税申报，完成后确认结果。"
        )
        return self._incomplete(
            attention="overdue" if self.today > deadline else "todo",
            missing=(
                ["current_payroll_tax_import_export"]
                if current_export is None
                else ["external_individual_income_tax_submission"]
            ),
            action=action,
            deadline=obligation["deadline"],
        ) | {"obligation": obligation, "existing_export": current_export}

    def _materials_step(
        self, _organization: Organization, _period: AccountingPeriod, gates: dict[str, Any]
    ) -> dict[str, Any]:
        gate = gates["gates"]["non_bank_materials"]
        if gate["satisfied"]:
            return self._completed(
                [
                    {
                        "kind": "owner_period_material_confirmation",
                        "confirmation_id": gate["confirmation_id"],
                        "source_snapshot_hash": gate["source_snapshot_hash"],
                    }
                ]
            )
        return self._incomplete(
            state="stale" if gate["stale"] else "incomplete",
            missing=["non_bank_material_completeness"],
            action="请确认本期票据、个人代垫和其他非银行业务材料已全部提供。",
            close_gate=False,
        )

    def _close_step(
        self, organization: Organization, period: AccountingPeriod, gates: dict[str, Any]
    ) -> dict[str, Any]:
        if period.status == "closed":
            close = self.session.get(AccountingPeriodClose, period.close_id)
            backup_session = self.catalog_session or self.session
            backup = (
                backup_session.scalar(
                    select(AccountingPeriodCloseBackup).where(
                        AccountingPeriodCloseBackup.org_id == organization.id,
                        AccountingPeriodCloseBackup.close_id == period.close_id,
                    )
                )
                if period.close_id is not None
                else None
            )
            if backup is not None and backup.status == "completed":
                return self._completed(
                    [
                        {"kind": "period_close", "close_id": str(close.id) if close else None},
                        {"kind": "automatic_close_backup", "status": "completed"},
                    ]
                )
            return self._incomplete(
                state="completed_with_backup_failure",
                attention="todo",
                missing=["automatic_close_backup"],
                action="账期已关闭，但自动备份未完成；请按原关账请求续做备份。",
            )
        if period.end_date >= self.today:
            return self._incomplete(
                state="waiting",
                attention="waiting_dependency",
                missing=["period_not_ended"],
                action=None,
                ready=False,
            )
        durable_blockers = [
            code
            for code, gate in gates["gates"].items()
            if not gate["satisfied"]
        ]
        if durable_blockers:
            return self._incomplete(
                state="waiting",
                attention="waiting_dependency",
                missing=durable_blockers,
                action=None,
                ready=False,
                close_gate=False,
            )
        from .accounting_period_schemas import PreviewAccountingPeriodCloseRequest
        from .accounting_period_service import AccountingPeriodService

        preview = AccountingPeriodService(
            self.session, current_date=self.today
        ).preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=organization.id,
                period_id=period.id,
                closing_date=period.end_date,
            )
        )
        blockers = list(preview.data.get("blocker_codes", []))
        if blockers:
            return self._incomplete(
                state="waiting",
                attention="waiting_dependency",
                missing=blockers,
                action=None,
                ready=False,
                close_gate=False,
            )
        return self._incomplete(
            missing=["owner_close_approval"],
            action="本期会计完整性门禁已通过，请完成关账确认。",
        )

    def _periodic_reporting_step(
        self, organization: Organization, period: AccountingPeriod, _gates: dict[str, Any]
    ) -> dict[str, Any]:
        pending = []
        for candidate in self.session.scalars(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == organization.id,
                AccountingPeriod.status == "closed",
            )
            .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
        ):
            if organization.filing_cycle != "monthly" and candidate.calendar_month not in {
                3,
                6,
                9,
                12,
            }:
                continue
            candidate_obligation = self._periodic_obligation(organization, candidate)
            if self._current_obligation_confirmation(candidate_obligation) is None:
                pending.append(candidate_obligation)
        if pending:
            obligation = min(pending, key=lambda item: (item["deadline"], item["obligation_id"]))
            deadline = date.fromisoformat(obligation["deadline"])
            return self._incomplete(
                attention="overdue" if self.today > deadline else "todo",
                missing=["periodic_tax_and_financial_reporting_submission"],
                action="请完成最早未结的税费申报及财务报表报送，完成后确认结果。",
                deadline=obligation["deadline"],
            ) | {"obligation": obligation}
        applicable = organization.filing_cycle == "monthly" or period.calendar_month in {
            3,
            6,
            9,
            12,
        }
        if not applicable:
            return self._not_applicable("本期不是税费及财务报表申报期。")
        obligation = self._periodic_obligation(organization, period)
        if period.status != "closed":
            return self._incomplete(
                state="waiting",
                attention="waiting_dependency",
                missing=["period_close"],
                action=None,
                deadline=obligation["deadline"],
                ready=False,
            ) | {"obligation": obligation}
        confirmation = self._current_obligation_confirmation(obligation)
        if confirmation is not None:
            return self._completed(
                [
                    {
                        "kind": "external_obligation_confirmation",
                        "confirmation_id": str(confirmation.id),
                        "obligation_id": obligation["obligation_id"],
                    }
                ]
            )
        deadline = date.fromisoformat(obligation["deadline"])
        return self._incomplete(
            attention="overdue" if self.today > deadline else "todo",
            missing=["periodic_tax_and_financial_reporting_submission"],
            action="请完成本期税费申报及财务报表报送，完成后确认结果。",
            deadline=obligation["deadline"],
        ) | {"obligation": obligation}

    def _annual_eit_step(
        self, organization: Organization, period: AccountingPeriod, _gates: dict[str, Any]
    ) -> dict[str, Any]:
        return self._annual_step(organization, period, code="annual_enterprise_income_tax")

    def _annual_business_report_step(
        self, organization: Organization, period: AccountingPeriod, _gates: dict[str, Any]
    ) -> dict[str, Any]:
        return self._annual_step(organization, period, code="annual_business_report")

    def _annual_step(
        self, organization: Organization, _period: AccountingPeriod, *, code: str
    ) -> dict[str, Any]:
        establishment = self._establishment_date(organization.id)
        if establishment is None:
            return self._incomplete(
                missing=["organization_establishment_date"],
                action="请确认公司成立日期，以判断年度法定义务是否适用。",
            )
        report_years = list(range(establishment.year, self.today.year))
        if not report_years:
            return self._not_applicable("公司尚未进入首个年度报告期。")
        obligations = [
            self._annual_obligation(organization, code, report_year)
            for report_year in report_years
        ]
        pending = [
            obligation
            for obligation in obligations
            if self._current_obligation_confirmation(obligation) is None
        ]
        if not pending:
            obligation = obligations[-1]
            confirmation = self._current_obligation_confirmation(obligation)
            assert confirmation is not None
            return self._completed(
                [
                    {
                        "kind": "external_obligation_confirmation",
                        "confirmation_id": str(confirmation.id),
                        "obligation_id": obligation["obligation_id"],
                    }
                ]
            )
        obligation = min(pending, key=lambda item: (item["deadline"], item["obligation_id"]))
        report_year = int(obligation["scope_identity"])
        deadline = date.fromisoformat(obligation["deadline"])
        label = "企业所得税年度汇算清缴" if code == "annual_enterprise_income_tax" else "工商年报"
        return self._incomplete(
            attention="overdue" if self.today > deadline else "todo",
            missing=[code],
            action=f"请完成 {report_year} 年度{label}，完成后确认结果。",
            deadline=obligation["deadline"],
        ) | {"obligation": obligation}

    @staticmethod
    def _completed(proof: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "completion_state": "completed",
            "attention_state": "normal",
            "close_gate_satisfied": True,
            "completion_proof": proof,
            "missing_facts": [],
            "next_owner_action": None,
            "_ready": False,
        }

    @staticmethod
    def _not_applicable(reason: str) -> dict[str, Any]:
        return {
            "completion_state": "not_applicable",
            "attention_state": "normal",
            "close_gate_satisfied": True,
            "completion_proof": [{"kind": "not_applicable", "reason": reason}],
            "missing_facts": [],
            "next_owner_action": None,
            "_ready": False,
        }

    @staticmethod
    def _incomplete(
        *,
        missing: list[str],
        action: str | None,
        state: str = "incomplete",
        attention: str = "todo",
        deadline: str | None = None,
        ready: bool = True,
        close_gate: bool = True,
    ) -> dict[str, Any]:
        return {
            "completion_state": state,
            "attention_state": attention,
            "close_gate_satisfied": close_gate,
            "deadline": deadline,
            "completion_proof": [],
            "missing_facts": missing,
            "next_owner_action": action,
            "_ready": ready,
        }

    @staticmethod
    def _base_symbol(step: dict[str, Any]) -> str:
        if step["completion_state"] == "not_applicable":
            return "➖"
        if step["attention_state"] in {"due", "overdue"}:
            return "⏰"
        if step["completion_state"] == "completed":
            return "✅"
        return "⬜"

    @staticmethod
    def _select_current_action(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
        for step in steps:
            if step["completion_state"] in {"completed", "not_applicable"}:
                continue
            if not step.get("_ready") or step.get("next_owner_action") is None:
                continue
            if step["attention_state"] not in {"due", "overdue"}:
                step["symbol"] = "🔄"
            return {
                "step_order": step["order"],
                "step_code": step["code"],
                "label": step["label"],
                "owner_action": step["next_owner_action"],
                "deadline": step.get("deadline"),
            }
        return None

    def _confirm_period_fact(
        self,
        *,
        request: Any,
        period: AccountingPeriod,
        fact_type: str,
        state: str,
        source_snapshot_hash: str,
        source_snapshot: dict[str, Any],
        supersedes_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        command = (
            "finance_confirm_workforce_review"
            if fact_type == "workforce_review"
            else "finance_confirm_period_material_completeness"
        )
        payload_hash = self._request_hash(command, request)
        replay = self._idempotent_replay(
            OwnerPeriodConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "OWNER_PERIOD_CONFIRMATION_IDEMPOTENCY_PAYLOAD_MISMATCH",
        )
        if replay is not None:
            return replay
        active = self._active_period_confirmation(request.org_id, period.id, fact_type)
        invalid = self._validate_supersedes(
            active,
            supersedes_id,
            "OWNER_PERIOD_CONFIRMATION_CORRECTION_REQUIRES_SUPERSEDES",
            "INVALID_OWNER_PERIOD_CONFIRMATION_SUPERSEDES",
        )
        if invalid is not None:
            return invalid
        try:
            evidence = self._evidence_snapshot(request.org_id, request.evidence_references)
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        row = OwnerPeriodConfirmation(
            org_id=request.org_id,
            period_id=period.id,
            fact_type=fact_type,
            confirmation_state=state,
            source_snapshot_hash=source_snapshot_hash,
            source_snapshot=source_snapshot,
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            confirmation_note=request.confirmation_note,
            evidence_snapshot=evidence,
            supersedes_id=active.id if active is not None else None,
        )
        result = self._persist_row(
            row,
            OwnerPeriodConfirmation,
            request.org_id,
            request.idempotency_key,
            payload_hash,
            "OWNER_PERIOD_CONFIRMATION_CONCURRENT_WRITE_CONFLICT",
        )
        if result is not None:
            return result
        self._audit(
            request.org_id,
            "owner_period_fact_confirmed",
            {
                "confirmation_id": str(row.id),
                "period_id": str(period.id),
                "fact_type": fact_type,
                "source_snapshot_hash": source_snapshot_hash,
            },
        )
        return {
            "status": "confirmed",
            "confirmation_id": str(row.id),
            "fact_type": fact_type,
            "source_snapshot_hash": source_snapshot_hash,
            "verification_level": "evidence_attached" if evidence else "owner_confirmed",
        }

    def _workforce_snapshot(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> dict[str, Any]:
        finance = FinanceService(self.session)
        ym = YearMonth(period.calendar_year, period.calendar_month)
        employees = list(
            self.session.scalars(
                select(Employee)
                .where(
                    Employee.org_id == org_id,
                    Employee.employment_start_date <= period.end_date,
                    (
                        Employee.employment_end_date.is_(None)
                        | (Employee.employment_end_date >= period.start_date)
                    ),
                )
                .order_by(Employee.employee_code, Employee.id)
            )
        )
        rows = []
        for employee in employees:
            profile = finance._effective_profile(employee.id, ym.end_date)
            rows.append(
                {
                    "employee_id": str(employee.id),
                    "employee_code": employee.employee_code,
                    "employment_start_date": employee.employment_start_date.isoformat(),
                    "employment_end_date": (
                        employee.employment_end_date.isoformat()
                        if employee.employment_end_date
                        else None
                    ),
                    "status": employee.status,
                    "tax_withholding_start_date": (
                        employee.tax_withholding_start_date.isoformat()
                        if employee.tax_withholding_start_date
                        else None
                    ),
                    "payroll_profile": (
                        {
                            "id": str(profile.id),
                            "effective_from": profile.effective_from.isoformat(),
                            "effective_to": (
                                profile.effective_to.isoformat() if profile.effective_to else None
                            ),
                            "expense_role": profile.expense_role,
                            "social_insurance_base_fen": profile.social_insurance_base_fen,
                            "housing_fund_base_fen": profile.housing_fund_base_fen,
                            "social_insurance_participating": (
                                profile.social_insurance_participating
                            ),
                            "housing_fund_participating": profile.housing_fund_participating,
                            "resident_employee": profile.resident_employee,
                        }
                        if profile
                        else None
                    ),
                }
            )
        data = {
            "version": "workforce_snapshot_v1",
            "period_id": str(period.id),
            "period": self._period_month(period),
            "employees": rows,
        }
        return {"hash": canonical_sha256(data), "data": data}

    def _material_snapshot(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> dict[str, Any]:
        events = list(
            self.session.scalars(
                select(BusinessEvent)
                .where(
                    BusinessEvent.org_id == org_id,
                    BusinessEvent.posting_date.between(period.start_date, period.end_date),
                )
                .order_by(BusinessEvent.posting_date, BusinessEvent.id)
            )
        )
        event_ids = [event.id for event in events]
        evidence_rows = (
            list(
                self.session.execute(
                    select(Evidence)
                    .where(
                        Evidence.org_id == org_id,
                        exists().where(
                            event_evidence.c.org_id == Evidence.org_id,
                            event_evidence.c.evidence_id == Evidence.id,
                            event_evidence.c.event_id.in_(event_ids),
                        ),
                    )
                    .order_by(Evidence.id)
                ).scalars()
            )
            if event_ids
            else []
        )
        data = {
            "version": "period_activity_material_snapshot_v1",
            "period_id": str(period.id),
            "events": [
                {
                    "id": str(event.id),
                    "event_type": event.event_type,
                    "status": event.status,
                    "posting_date": event.posting_date.isoformat(),
                    "reversed_by_event_id": (
                        str(event.reversed_by_event_id) if event.reversed_by_event_id else None
                    ),
                    "facts_hash": canonical_sha256(event.facts),
                }
                for event in events
            ],
            "event_evidence": [
                {"id": str(item.id), "sha256": item.sha256} for item in evidence_rows
            ],
        }
        return {"hash": canonical_sha256(data), "data": data}

    def _contribution_snapshot(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> dict[str, Any]:
        finance = FinanceService(self.session)
        ym = YearMonth(period.calendar_year, period.calendar_month)
        employees = list(
            self.session.scalars(
                select(Employee)
                .where(
                    Employee.org_id == org_id,
                    Employee.status == "active",
                    Employee.employment_start_date <= period.end_date,
                    (
                        Employee.employment_end_date.is_(None)
                        | (Employee.employment_end_date >= period.start_date)
                    ),
                )
                .order_by(Employee.employee_code, Employee.id)
            )
        )
        missing: list[dict[str, Any]] = []
        policy = finance._effective_payroll_policy(org_id, ym.end_date)
        if employees and policy is None:
            missing.append(
                {
                    "code": "payroll_policy",
                    "fields": ["contribution_policy_version"],
                    "message": "an effective contribution policy is required",
                }
            )
        calculation_rows: list[dict[str, Any]] = []
        totals = {
            "employee_social_insurance_fen": 0,
            "employer_social_insurance_fen": 0,
            "employee_housing_fund_fen": 0,
            "employer_housing_fund_fen": 0,
        }
        contribution_policy = None
        if policy is not None:
            try:
                contribution_policy, _, _ = finance._calculator_policies(policy)
            except CalculationValidationError as exc:
                missing.append(
                    {
                        "code": exc.code,
                        "fields": ["contribution_policy_version"],
                        "message": "the contribution policy cannot be calculated",
                    }
                )
        for employee in employees:
            profile = finance._effective_profile(employee.id, ym.end_date)
            if profile is None:
                missing.append(
                    {
                        "code": "employee_payroll_profile",
                        "fields": ["employee_payroll_profile_version"],
                        "employee_id": str(employee.id),
                        "message": "an effective employee payroll profile is required",
                    }
                )
                continue
            if contribution_policy is None:
                continue
            actuals = finance._active_contribution_actual_items(
                org_id, employee.id, self._period_month(period)
            )
            try:
                calculated = calculate_contributions(
                    contribution_policy,
                    ContributionBases(
                        profile.social_insurance_base_fen,
                        profile.housing_fund_base_fen,
                        profile.social_insurance_participating,
                        profile.housing_fund_participating,
                    ),
                    ym.end_date,
                )
                calculated = apply_contribution_actuals(
                    calculated,
                    tuple(
                        ContributionActualOverride(
                            actual_item_id=str(item.id),
                            code=item.insurance_kind,
                            base_kind=ContributionBaseKind(item.contribution_group),
                            actual_state=item.actual_state,
                            employee_amount_fen=item.employee_amount_fen,
                            employer_amount_fen=item.employer_amount_fen,
                        )
                        for item in actuals
                    ),
                )
            except (CalculationValidationError, NeedsInformationError) as exc:
                missing.append(
                    {
                        "code": getattr(exc, "code", "contribution_bases"),
                        "fields": ["employee_payroll_profile_version"],
                        "employee_id": str(employee.id),
                        "message": "employee contribution calculation needs information",
                    }
                )
                continue
            row = {
                "employee_id": str(employee.id),
                "employee_code": employee.employee_code,
                "profile_id": str(profile.id),
                "actual_item_ids": [str(item.id) for item in actuals],
                "lines": [
                    {
                        "contribution_group": str(line.base_kind),
                        "insurance_kind": line.code,
                        "employee_amount_fen": line.employee_contribution_fen,
                        "employer_amount_fen": line.employer_contribution_fen,
                    }
                    for line in calculated.lines
                ],
                "employee_social_insurance_fen": calculated.employee_social_insurance_fen,
                "employer_social_insurance_fen": calculated.employer_social_insurance_fen,
                "employee_housing_fund_fen": calculated.employee_housing_fund_fen,
                "employer_housing_fund_fen": calculated.employer_housing_fund_fen,
            }
            for key in totals:
                totals[key] += row[key]
            calculation_rows.append(row)
        calculation = {
            "version": "payroll_contribution_assessment_v1",
            "period_id": str(period.id),
            "contribution_period": self._period_month(period),
            "policy": FinanceService._policy_snapshot(policy) if policy else None,
            "employees": calculation_rows,
            "totals": totals,
            "source_urls": [
                *( [policy.source_url] if policy else [] ),
                _SOCIAL_SOURCE_URL,
            ],
        }
        return {
            "active_employee_count": len(employees),
            "missing_information": missing,
            "calculation_hash": canonical_sha256(calculation) if not missing else None,
            "calculation": calculation,
        }

    def _posted_payroll_matches_contribution(
        self,
        org_id: uuid.UUID,
        period: AccountingPeriod,
        calculation: dict[str, Any],
    ) -> dict[str, Any]:
        batch = self.session.scalar(
            select(PayrollBatch)
            .where(
                PayrollBatch.org_id == org_id,
                PayrollBatch.batch_kind == "regular",
                PayrollBatch.payroll_period == self._period_month(period),
                PayrollBatch.status == "posted",
                PayrollBatch.reversal_of_batch_id.is_(None),
            )
            .order_by(PayrollBatch.version.desc(), PayrollBatch.id.desc())
            .limit(1)
        )
        if batch is None:
            return {
                "satisfied": False,
                "batch_id": None,
                "calculation_hash": None,
                "reason": "posted_regular_payroll_missing",
            }
        lines = list(
            self.session.scalars(
                select(PayrollLine)
                .where(
                    PayrollLine.org_id == org_id,
                    PayrollLine.payroll_batch_id == batch.id,
                )
                .order_by(PayrollLine.employee_id, PayrollLine.id)
            )
        )
        expected_employees = {
            row["employee_id"]: row for row in calculation["employees"]
        }
        actual_employees = {str(line.employee_id): line for line in lines}
        profile_match = set(expected_employees) == set(actual_employees) and all(
            str(actual_employees[employee_id].employee_payroll_profile_version_id)
            == row["profile_id"]
            for employee_id, row in expected_employees.items()
        )
        expected_actual_ids = {
            item_id
            for row in calculation["employees"]
            for item_id in row["actual_item_ids"]
        }
        used_actual_ids = {
            str(item_id)
            for item_id in self.session.scalars(
                select(PayrollContributionActualUse.actual_item_id).where(
                    PayrollContributionActualUse.org_id == org_id,
                    PayrollContributionActualUse.payroll_batch_id == batch.id,
                )
            )
        }
        totals = calculation["totals"]
        amount_match = (
            sum(line.employee_social_insurance_fen for line in lines)
            == totals["employee_social_insurance_fen"]
            and sum(line.employer_social_insurance_fen for line in lines)
            == totals["employer_social_insurance_fen"]
            and sum(line.employee_housing_fund_fen for line in lines)
            == totals["employee_housing_fund_fen"]
            and sum(line.employer_housing_fund_fen for line in lines)
            == totals["employer_housing_fund_fen"]
        )
        policy_id = (batch.policy_snapshot.get("contribution_policy") or {}).get("id")
        expected_policy_id = (calculation.get("policy") or {}).get("id")
        satisfied = (
            profile_match
            and expected_actual_ids == used_actual_ids
            and amount_match
            and policy_id == expected_policy_id
        )
        return {
            "satisfied": satisfied,
            "batch_id": str(batch.id),
            "calculation_hash": batch.calculation_hash,
            "reason": "same_snapshot" if satisfied else "snapshot_mismatch",
        }

    def _posted_payroll_source_snapshot(
        self, org_id: uuid.UUID, period: AccountingPeriod
    ) -> dict[str, Any]:
        rows = self.session.execute(
            select(PayrollBatch, PayrollLine)
            .join(
                PayrollLine,
                (PayrollLine.org_id == PayrollBatch.org_id)
                & (PayrollLine.payroll_batch_id == PayrollBatch.id),
            )
            .where(
                PayrollBatch.org_id == org_id,
                PayrollBatch.batch_kind == "regular",
                PayrollBatch.payroll_period == self._period_month(period),
                PayrollBatch.status == "posted",
                PayrollBatch.reversal_of_batch_id.is_(None),
                PayrollLine.wage_tax_declaration_state == "declared",
            )
            .order_by(PayrollBatch.id, PayrollLine.employee_id, PayrollLine.id)
        ).all()
        data = {
            "version": "payroll_tax_import_source_v1",
            "org_id": str(org_id),
            "payroll_period": self._period_month(period),
            "lines": [
                {
                    "batch_id": str(batch.id),
                    "batch_calculation_hash": batch.calculation_hash,
                    "employee_id": str(line.employee_id),
                    "profile_id": str(line.employee_payroll_profile_version_id),
                    "tax_reported_salary_fen": line.tax_reported_salary_fen,
                    "special_additional_deduction_fen": line.special_additional_deduction_fen,
                    "other_legal_deduction_fen": line.other_legal_deduction_fen,
                    "individual_income_tax_fen": line.individual_income_tax_fen,
                }
                for batch, line in rows
            ],
        }
        return {
            "hash": canonical_sha256(data),
            "data": data,
            "declared_line_count": len(rows),
        }

    def _period_exports(self, org_id: uuid.UUID, payroll_period: str) -> list[dict[str, Any]]:
        rows = list(
            self.session.scalars(
                select(PayrollTaxImportExport)
                .where(
                    PayrollTaxImportExport.org_id == org_id,
                    PayrollTaxImportExport.payroll_period == payroll_period,
                )
                .order_by(
                    PayrollTaxImportExport.created_at.desc(),
                    PayrollTaxImportExport.id.desc(),
                )
            )
        )
        period = self.session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.calendar_year == int(payroll_period[:4]),
                AccountingPeriod.calendar_month == int(payroll_period[5:]),
            )
        )
        current_hash = (
            self._posted_payroll_source_snapshot(org_id, period)["hash"] if period else None
        )
        superseded_ids = {row.supersedes_id for row in rows if row.supersedes_id is not None}
        return [
            {
                "id": str(row.id),
                "file_name": row.file_name,
                "relative_storage_path": row.relative_storage_path,
                "sha256": row.file_sha256,
                "row_count": row.row_count,
                "payroll_source_hash": row.payroll_source_hash,
                "source_snapshot_hash": row.source_snapshot_hash,
                "current": row.id not in superseded_ids and row.payroll_source_hash == current_hash,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    def _obligation_catalog(self, organization: Organization) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        periods = list(
            self.session.scalars(
                select(AccountingPeriod)
                .where(AccountingPeriod.org_id == organization.id)
                .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
            )
        )
        for period in periods:
            source = self._posted_payroll_source_snapshot(organization.id, period)
            if source["declared_line_count"]:
                result.append(self._iit_obligation(organization, period))
            if organization.filing_cycle == "monthly" or period.calendar_month in {3, 6, 9, 12}:
                result.append(self._periodic_obligation(organization, period))
        establishment = self._establishment_date(organization.id)
        if establishment is not None:
            for report_year in range(establishment.year, self.today.year):
                result.append(
                    self._annual_obligation(
                        organization, "annual_enterprise_income_tax", report_year
                    )
                )
                result.append(
                    self._annual_obligation(organization, "annual_business_report", report_year)
                )
        return result

    def _iit_obligation(
        self, organization: Organization, period: AccountingPeriod
    ) -> dict[str, Any]:
        source = self._posted_payroll_source_snapshot(organization.id, period)
        deadline = self._next_month_deadline(period.end_date)
        return self._obligation(
            organization.id,
            "individual_income_tax",
            "month",
            self._period_month(period),
            deadline,
            {
                "payroll_source_hash": source["hash"],
                "period_id": str(period.id),
                "rule_version": "cn_iit_withholding_deadline_2026.1",
            },
            [_IIT_SOURCE_URL, _TAX_DEADLINE_SOURCE_URL],
        )

    def _periodic_obligation(
        self, organization: Organization, period: AccountingPeriod
    ) -> dict[str, Any]:
        deadline = self._next_month_deadline(period.end_date)
        close_hash = None
        if period.close_id is not None:
            close_hash = self.session.scalar(
                select(AccountingPeriodClose.calculation_hash).where(
                    AccountingPeriodClose.org_id == organization.id,
                    AccountingPeriodClose.id == period.close_id,
                )
            )
        scope = (
            self._period_month(period)
            if organization.filing_cycle == "monthly"
            else f"{period.calendar_year:04d}-Q{(period.calendar_month - 1) // 3 + 1}"
        )
        return self._obligation(
            organization.id,
            "periodic_tax_reporting",
            "month" if organization.filing_cycle == "monthly" else "quarter",
            scope,
            deadline,
            {
                "period_id": str(period.id),
                "period_close_hash": close_hash,
                "filing_cycle": organization.filing_cycle,
                "rule_version": "cn_periodic_tax_deadline_2026.1",
            },
            [_TAX_DEADLINE_SOURCE_URL],
        )

    def _annual_obligation(
        self, organization: Organization, code: str, report_year: int
    ) -> dict[str, Any]:
        establishment = self._establishment_date(organization.id)
        if code == "annual_enterprise_income_tax":
            deadline = date(report_year + 1, 5, 31)
            sources = [_TAX_DEADLINE_SOURCE_URL]
        else:
            deadline = date(report_year + 1, 6, 30)
            sources = [_BUSINESS_REPORT_SOURCE_URL]
        return self._obligation(
            organization.id,
            code,
            "year",
            str(report_year),
            deadline,
            {
                "report_year": report_year,
                "establishment_date": establishment.isoformat() if establishment else None,
                "rule_version": f"cn_{code}_deadline_2026.1",
            },
            sources,
        )

    @staticmethod
    def _obligation(
        org_id: uuid.UUID,
        code: str,
        scope: str,
        scope_identity: str,
        deadline: date,
        source: dict[str, Any],
        source_urls: list[str],
    ) -> dict[str, Any]:
        obligation_id = uuid.uuid5(
            _OBLIGATION_NAMESPACE, f"{org_id}:{code}:{scope}:{scope_identity}"
        )
        source_payload = {
            "org_id": str(org_id),
            "obligation_id": str(obligation_id),
            "code": code,
            "scope": scope,
            "scope_identity": scope_identity,
            "deadline": deadline.isoformat(),
            "source": source,
            "source_urls": source_urls,
        }
        return source_payload | {"source_snapshot_hash": canonical_sha256(source_payload)}

    def _current_obligation_confirmation(
        self, obligation: dict[str, Any]
    ) -> ExternalObligationConfirmation | None:
        row = self._active_obligation_confirmation(
            uuid.UUID(obligation["org_id"]),
            uuid.UUID(obligation["obligation_id"]),
        )
        return (
            row
            if row is not None
            and row.source_snapshot_hash == obligation["source_snapshot_hash"]
            else None
        )

    def _establishment_date(self, org_id: uuid.UUID) -> date | None:
        current = self._active_establishment_confirmation(org_id)
        if current is not None:
            return current.establishment_date
        opening = self.session.scalar(
            select(FinancialStatementOpeningBalanceConfirmation).where(
                FinancialStatementOpeningBalanceConfirmation.org_id == org_id
            )
        )
        return opening.establishment_date if opening is not None else None

    def _next_month_deadline(self, period_end: date) -> date:
        next_month = (
            date(period_end.year + 1, 1, 1)
            if period_end.month == 12
            else date(period_end.year, period_end.month + 1, 1)
        )
        if next_month.year == 2026:
            return _CN_2026_MONTHLY_FILING_DEADLINES[next_month.month]
        return date(next_month.year, next_month.month, 15)

    def _resolve_period(
        self, org_id: uuid.UUID, period_id: uuid.UUID | None
    ) -> AccountingPeriod | None:
        if period_id is not None:
            return self._period_for_org(org_id, period_id)
        open_period = self.session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.status == "open",
            )
            .order_by(AccountingPeriod.start_date, AccountingPeriod.id)
            .limit(1)
        )
        if open_period is not None:
            return open_period
        return self.session.scalar(
            select(AccountingPeriod)
            .where(AccountingPeriod.org_id == org_id)
            .order_by(AccountingPeriod.end_date.desc(), AccountingPeriod.id.desc())
            .limit(1)
        )

    def _period_for_org(
        self, org_id: uuid.UUID, period_id: uuid.UUID
    ) -> AccountingPeriod | None:
        return self.session.scalar(
            select(AccountingPeriod).where(
                AccountingPeriod.org_id == org_id,
                AccountingPeriod.id == period_id,
            )
        )

    @staticmethod
    def _period_month(period: AccountingPeriod) -> str:
        return f"{period.calendar_year:04d}-{period.calendar_month:02d}"

    def _period_payload(self, period: AccountingPeriod) -> dict[str, Any]:
        return {
            "id": str(period.id),
            "period": self._period_month(period),
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
            "status": period.status,
        }

    def _active_period_confirmation(
        self, org_id: uuid.UUID, period_id: uuid.UUID, fact_type: str
    ) -> OwnerPeriodConfirmation | None:
        successor = aliased(OwnerPeriodConfirmation)
        return self.session.scalar(
            select(OwnerPeriodConfirmation)
            .where(
                OwnerPeriodConfirmation.org_id == org_id,
                OwnerPeriodConfirmation.period_id == period_id,
                OwnerPeriodConfirmation.fact_type == fact_type,
                ~exists(
                    select(successor.id).where(
                        successor.supersedes_id == OwnerPeriodConfirmation.id
                    )
                ),
            )
            .order_by(OwnerPeriodConfirmation.created_at.desc(), OwnerPeriodConfirmation.id.desc())
            .limit(1)
        )

    def _active_contribution_confirmation(
        self, org_id: uuid.UUID, period_id: uuid.UUID
    ) -> PayrollContributionAssessmentConfirmation | None:
        successor = aliased(PayrollContributionAssessmentConfirmation)
        return self.session.scalar(
            select(PayrollContributionAssessmentConfirmation)
            .where(
                PayrollContributionAssessmentConfirmation.org_id == org_id,
                PayrollContributionAssessmentConfirmation.period_id == period_id,
                ~exists(
                    select(successor.id).where(
                        successor.supersedes_id
                        == PayrollContributionAssessmentConfirmation.id
                    )
                ),
            )
            .order_by(
                PayrollContributionAssessmentConfirmation.created_at.desc(),
                PayrollContributionAssessmentConfirmation.id.desc(),
            )
            .limit(1)
        )

    def _active_obligation_confirmation(
        self, org_id: uuid.UUID | None, obligation_id: uuid.UUID
    ) -> ExternalObligationConfirmation | None:
        successor = aliased(ExternalObligationConfirmation)
        conditions = [
            ExternalObligationConfirmation.obligation_id == obligation_id,
            ~exists(
                select(successor.id).where(
                    successor.supersedes_id == ExternalObligationConfirmation.id
                )
            ),
        ]
        if org_id is not None:
            conditions.append(ExternalObligationConfirmation.org_id == org_id)
        return self.session.scalar(
            select(ExternalObligationConfirmation)
            .where(*conditions)
            .order_by(
                ExternalObligationConfirmation.created_at.desc(),
                ExternalObligationConfirmation.id.desc(),
            )
            .limit(1)
        )

    def _active_establishment_confirmation(
        self, org_id: uuid.UUID
    ) -> OrganizationEstablishmentConfirmation | None:
        successor = aliased(OrganizationEstablishmentConfirmation)
        return self.session.scalar(
            select(OrganizationEstablishmentConfirmation)
            .where(
                OrganizationEstablishmentConfirmation.org_id == org_id,
                ~exists(
                    select(successor.id).where(
                        successor.supersedes_id == OrganizationEstablishmentConfirmation.id
                    )
                ),
            )
            .order_by(
                OrganizationEstablishmentConfirmation.created_at.desc(),
                OrganizationEstablishmentConfirmation.id.desc(),
            )
            .limit(1)
        )

    def _evidence_snapshot(
        self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]
    ) -> list[dict[str, Any]]:
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("OWNER_WORKFLOW_DUPLICATE_EVIDENCE_REFERENCE")
        if not evidence_ids:
            return []
        evidence = list(
            self.session.scalars(
                select(Evidence)
                .where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
                .order_by(Evidence.id)
            )
        )
        if len(evidence) != len(evidence_ids):
            raise ValueError("OWNER_WORKFLOW_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")
        return [{"id": str(item.id), "sha256": item.sha256} for item in evidence]

    @staticmethod
    def _request_hash(command: str, request: Any) -> str:
        return canonical_sha256(
            {"command": command, "request": request.model_dump(mode="json")}
        )

    def _idempotent_replay(
        self,
        model: Any,
        org_id: uuid.UUID,
        idempotency_key: str,
        payload_hash: str,
        mismatch_code: str,
    ) -> dict[str, Any] | None:
        existing = self.session.scalar(
            select(model).where(
                model.org_id == org_id,
                model.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            return None
        if existing.request_payload_hash != payload_hash:
            return {"status": "rejected", "errors": [mismatch_code]}
        return {
            "status": "confirmed",
            "confirmation_id": str(existing.id),
            "idempotent_replay": True,
        }

    @staticmethod
    def _validate_supersedes(
        active: Any,
        requested: uuid.UUID | None,
        requires_code: str,
        invalid_code: str,
    ) -> dict[str, Any] | None:
        if active is None and requested is None:
            return None
        if active is not None and requested is None:
            return {"status": "rejected", "errors": [requires_code]}
        if active is None or active.id != requested:
            return {"status": "rejected", "errors": [invalid_code]}
        return None

    def _persist_row(
        self,
        row: Any,
        model: Any,
        org_id: uuid.UUID,
        idempotency_key: str,
        payload_hash: str,
        conflict_code: str,
    ) -> dict[str, Any] | None:
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            concurrent = self.session.scalar(
                select(model).where(
                    model.org_id == org_id,
                    model.idempotency_key == idempotency_key,
                )
            )
            if concurrent is not None and concurrent.request_payload_hash == payload_hash:
                return {
                    "status": "confirmed",
                    "confirmation_id": str(concurrent.id),
                    "idempotent_replay": True,
                }
            return {"status": "rejected", "errors": [conflict_code]}
        return None

    def _audit(self, org_id: uuid.UUID, action: str, details: dict[str, Any]) -> None:
        self.session.add(AuditLog(org_id=org_id, action=action, details=details))
