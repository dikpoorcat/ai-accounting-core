"""Exact read-only fact grammars of the declared 0c8abb draft source.

Only stored shape and source-reading declarations are retained. No retired tax
or loan calculator is registered. The four source files belong to fixed SOURCE24:
886bfebf8cef5773c5e04da563b69ff5a9d9446a86a88d930ba3622cb961fa8e (payroll),
7cebd9a884957320f3af3d743eb4bd1c11016312b076cbf2e5280bbfae5c708d (plan),
e897aabdeb691e1684f459ebc38b0f2a76dd0a78e74bf4ac9c4b386f08ed6b9f (materials),
b0d5e27719d83f5e78c9f13d10d44bdecd9a90d9b5141a6dd9c54e11b62323a1 (loan).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

from pydantic import Field, model_validator

from .contracts import Context, Fact, FactVersion, KernelError, NeedsInformation, Read, Registry
from .domains.money import ACTUAL_PAYMENT_KINDS
from .materials import Detail, MaterialResolution
from .payroll_confirmation import FactRevisionReference, revision_reference
from .payroll_history import (
    PAYROLL_KINDS,
    ExpenseClass,
    Identifier,
    PayrollContributionActual,
    PayrollContributionPolicy,
    PayrollIncomeTaxPolicy,
    PayrollOpeningState,
    PayrollProfile,
    PayrollWithholdingActual,
    employee_month,
    employee_year,
    first_wage_read,
)
from .types import ActualDate, Fen, NonNegativeFen, SubjectId, YearMonth

PLAN_KINDS = ("payroll_plan_v2", "payroll_plan_bounded")
NOTICE_KIND = "payroll_change_notice_v2"
NO_CHANGE_KIND = "payroll_no_change_v2"
PROFILE_KIND = "payroll_profile"
CONTRIBUTION_POLICY_KIND = "payroll_contribution_policy"
INCOME_TAX_POLICY_KIND = "payroll_income_tax_policy"

SOURCE_FINGERPRINT = "0c8abb83f06fa66266ba5802f5d70cabed3b861015a977757e0538f14a8ee2e7"


def _previous(period: YearMonth) -> YearMonth:
    return YearMonth.from_ordinal(period.ordinal - 1)

def _employee_month(employee_id: str, period: YearMonth) -> str:
    return f"employee:{employee_id}:month:{period}"

def confirmation_reads(fact: Any) -> tuple[Read, ...]:
    """Declare every input the confirmation decision may select, including an empty roster."""

    scope = _employee_month(fact.employee_id, fact.period)
    previous_scope = _employee_month(fact.employee_id, _previous(fact.period))
    return tuple(
        dict.fromkeys(
            (
                *(Read("fact", kind, scope) for kind in PLAN_KINDS),
                Read("fact", NOTICE_KIND, scope),
                Read("fact", NO_CHANGE_KIND, scope),
                Read("fact", PROFILE_KIND, "*"),
                *(Read("fact", kind, previous_scope) for kind in PAYROLL_KINDS),
                *(Read("calculation", kind, previous_scope) for kind in PAYROLL_KINDS),
            )
        )
    )

class PayrollPreRevision(Fact):
    material_amount_aliases: ClassVar[dict[str, str]] = {
        "fact.accounting_gross_salary_fen": "result.gross_fen"
    }
    kind: ClassVar[str] = "payroll"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: Identifier
    profile_id: SubjectId
    contribution_policy_id: SubjectId
    income_tax_policy_id: SubjectId
    accounting_gross_salary_fen: NonNegativeFen
    tax_reported_salary_fen: NonNegativeFen
    tax_exempt_income_fen: NonNegativeFen
    special_additional_deduction_fen: NonNegativeFen
    other_legal_deduction_fen: NonNegativeFen
    tax_relief_fen: NonNegativeFen
    expense_class: ExpenseClass
    contribution_basis: Literal["policy_until_actual", "actual_required"]

    def scopes(self) -> tuple[str, ...]:
        return (employee_month(self.employee_id, self.period),)

    def reads(self) -> tuple[Read, ...]:
        from .identity_corrections import opening_binding_reads

        current_scope = employee_month(self.employee_id, self.period)
        year_scope = employee_year(self.employee_id, self.period)
        prior = tuple(
            Read("calculation", kind, employee_month(self.employee_id, f"{self.period[:4]}-{m:02}"))
            for m in range(1, int(self.period[5:]))
            for kind in (*PAYROLL_KINDS, "annual_bonus")
        )
        reads = (
            *(Read("fact", kind, current_scope) for kind in PAYROLL_KINDS),
            Read("fact", PayrollProfile.kind, f"@{self.profile_id}"),
            Read("fact", PayrollContributionPolicy.kind, f"@{self.contribution_policy_id}"),
            Read("fact", PayrollIncomeTaxPolicy.kind, f"@{self.income_tax_policy_id}"),
            Read("fact", PayrollContributionActual.kind, current_scope),
            Read("fact", PayrollOpeningState.kind, year_scope),
            Read("fact", "opening_payroll_state", year_scope),
            Read("calculation", "opening_payroll_state", year_scope),
            *opening_binding_reads(year_scope),
            first_wage_read(self.employee_id, self.period),
            Read("fact", PayrollWithholdingActual.kind, current_scope),
            *prior,
        )
        return tuple(dict.fromkeys((*reads, *confirmation_reads(self))))

class PayrollPlanPreRevision(Fact):
    kind: ClassVar[str] = "payroll_plan_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: str = Field(min_length=1)
    payroll: PayrollPreRevision
    profile_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "confirmed_payroll_profile_revision",
                "reusable_sources": ["payroll_profile"],
                "constraint": "正式工资只采用负责人方案精确引用的员工档案版本",
            }
        }
    )
    contribution_policy_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "confirmed_contribution_policy_revision",
                "reusable_sources": ["payroll_contribution_policy"],
                "constraint": "正式工资只采用负责人方案精确引用的社保公积金规则版本",
            }
        }
    )
    income_tax_policy_revision: FactRevisionReference | None = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "confirmed_adopted_income_tax_policy_revision",
                "reusable_sources": ["payroll_income_tax_policy"],
                "constraint": "已开始扣缴时必须精确引用；尚未开始且未采用时必须为空",
            }
        }
    )
    change_notice_revisions: tuple[FactRevisionReference, ...] = Field(
        default=(),
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "confirmed_known_change_notice_versions",
                "reusable_sources": ["payroll_change_notice_v2", "owner_payroll_materials"],
                "constraint": "必须完整精确覆盖该员工本月全部有效变更通知",
            }
        },
    )

    def scopes(self):
        return (str(self.period), employee_month(self.employee_id, self.period))

    @model_validator(mode="after")
    def same_employee(self):
        if self.payroll.employee_id != self.employee_id or self.payroll.period != self.period:
            raise ValueError("monthly plan identity differs from wage facts")
        if self.profile_revision.subject_id != self.payroll.profile_id:
            raise ValueError("monthly plan profile reference differs from wage facts")
        if self.contribution_policy_revision.subject_id != self.payroll.contribution_policy_id:
            raise ValueError("monthly plan contribution policy reference differs from wage facts")
        if (
            self.income_tax_policy_revision is not None
            and self.income_tax_policy_revision.subject_id != self.payroll.income_tax_policy_id
        ):
            raise ValueError("monthly plan income-tax policy reference differs from wage facts")
        notice_refs = [(item.subject_id, item.revision) for item in self.change_notice_revisions]
        if notice_refs != sorted(set(notice_refs)):
            raise ValueError("monthly plan change-notice references must be unique and sorted")
        return self

class MaterialLinkPreReference(Detail):
    subject_id: Identifier
    fact_kind: Identifier
    fact_id: Identifier
    calculation_id: Identifier
    amount_field: Annotated[str, Field(pattern=r"^(fact|result)\.[a-z][a-z0-9_]*_fen$")]
    amount_fen: Fen
    recognition_period: YearMonth

class MaterialResolutionPreReference(MaterialResolution):
    kind: ClassVar[str] = "material_resolution_v2"
    lane: ClassVar[str] = "material"
    identity_fields: ClassVar[tuple[str, ...]] = ("source_id", "location")
    source_id: Identifier
    source_fact_id: Identifier
    location: Identifier
    treatment: Literal[
        "recognize", "duplicate", "supporting", "no_accounting", "other_period", "control_total"
    ]
    amount_fen: Fen | None = None
    recognition_period: YearMonth | None = None
    links: tuple[MaterialLinkPreReference, ...] = Field(
        default=(),
        description="认账金额分配；重复资料仅精确选择目标联合组的完整正式关联。",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "contextual",
                "meaning": "material_result_association_or_exact_group_duplicate_selector",
                "constraint": (
                    "认账为金额分配；duplicate仅选择目标group完整typed link，不新增业务或消耗容量"
                ),
                "reusable_sources": [
                    "current_material_group_resolution",
                    "current_published_business_result",
                ],
            }
        },
    )
    duplicate_source_id: Identifier | None = None
    duplicate_location: Identifier | None = None
    reason: str | None = None
    non_accounting_reason: (
        Literal[
            "forecast",
            "balance_control",
            "not_company_business",
            "cancelled_before_recognition",
            "zero_amount",
        ]
        | None
    ) = None

    def scopes(self):
        return (
            str(self.period),
            "material-source:" + self.source_id,
            *(
                ("material-period:" + str(self.recognition_period),)
                if self.recognition_period
                else ()
            ),
            *("material-period:" + str(link.recognition_period) for link in self.links),
            *("material-business:" + link.subject_id for link in self.links),
        )

class LoanInterestPreReference(Fact):
    kind: ClassVar[str] = "loan_interest"
    identity_fields: ClassVar[tuple[str, ...]] = ("drawdown_id", "agreement_id")
    drawdown_id: SubjectId
    agreement_id: SubjectId
    period_start: ActualDate
    period_end_exclusive: ActualDate

    @model_validator(mode="after")
    def actual_period(self):
        start, end = (
            date.fromisoformat(self.period_start),
            date.fromisoformat(self.period_end_exclusive),
        )
        if (
            end <= start
            or self.period_start.period != self.period
            or YearMonth((end - timedelta(days=1)).isoformat()[:7]) != self.period
        ):
            raise ValueError("interest interval must fall within its accounting month")
        return self

    def scopes(self):
        return (str(self.period), f"loan:{self.drawdown_id}")

    def reads(self):
        repayment_scope = f"payment:loan_drawdown:{self.drawdown_id}:principal"
        from .identity_corrections import opening_binding_reads

        opening_scope = f"payment:opening_loan:{self.drawdown_id}:principal"
        before = YearMonth.from_ordinal(self.period.ordinal + 1)
        return (
            Read("fact", "loan_agreement", f"@{self.agreement_id}"),
            Read("fact", "loan_drawdown", f"@{self.drawdown_id}"),
            Read("calculation", "loan_drawdown", f"@{self.drawdown_id}"),
            Read("fact", "opening_loan", f"@{self.drawdown_id}"),
            Read("calculation", "opening_loan", f"@{self.drawdown_id}"),
            *opening_binding_reads("opening-binding:" + self.drawdown_id),
            *(Read("fact", kind, repayment_scope, before) for kind in ACTUAL_PAYMENT_KINDS),
            Read("fact", "employee_advance", repayment_scope, before),
            *(
                Read("fact", kind, opening_scope, before)
                for kind in (*ACTUAL_PAYMENT_KINDS, "employee_advance")
            ),
            Read("fact", self.kind, f"loan:{self.drawdown_id}"),
        )

HISTORICAL_BODIES = {
    "payroll": (PayrollPreRevision, "fact_payroll_pre_revision"),
    "payroll_plan_v2": (PayrollPlanPreRevision, "fact_payroll_plan_v2_pre_revision"),
    "material_resolution_v2": (
        MaterialResolutionPreReference, "fact_material_resolution_v2_pre_reference",
    ),
    "loan_interest": (LoanInterestPreReference, "fact_loan_interest_pre_reference"),
}


def source_registry(registry):
    """Build the exact declared source reader without any retired evaluator."""
    from .development_contracts import validate_draft_contract
    from .schema_bundle import APPLICATION_ID, FAMILY

    path = Path(__file__).with_name("schema_contracts") / "development" / "company"
    contract = json.loads((path / (SOURCE_FINGERPRINT + ".json")).read_text("utf-8"))
    validate_draft_contract(contract, family=FAMILY, application_id=APPLICATION_ID)
    if contract["sha256"] != SOURCE_FINGERPRINT:
        raise ValueError("historical source contract identity mismatch")
    tables = {item["name"] for item in contract["objects"] if item["type"] == "table"}
    result = Registry()
    for kind in registry.models:
        if kind in HISTORICAL_BODIES:
            model, _ = HISTORICAL_BODIES[kind]
            result.register(model)
            continue
        bodies = tuple((model, table) for model, table in registry.fact_bodies(kind)
                       if table in tables)
        if not bodies:
            continue
        model, table = bodies[0]
        result.register(model, registry.evaluators.get(kind), table=table, historical=bodies[1:])
    result.historical_only = registry.historical_only | HISTORICAL_BODIES.keys()
    result.accounting_projectors = dict(registry.accounting_projectors)
    result.accounting_consumers = dict(registry.accounting_consumers)
    result.readiness = dict(registry.readiness)
    result.snapshot_readiness = dict(registry.snapshot_readiness)
    return result


def _same_payload(left, right) -> bool:
    return left.kind == right.kind and left.model_dump(mode="json") == right.model_dump(mode="json")

def _assert_ref(reference: FactRevisionReference, source: FactVersion, field: str) -> None:
    if revision_reference(source) != reference:
        raise NeedsInformation(field, "工资确认引用的来源版本已变化，需要重新确认")

def _tax_started(fact: Any, profile: Any) -> bool:
    first = max(YearMonth(f"{fact.period[:4]}-01"), YearMonth(profile.withholding_start_date[:7]))
    return fact.period >= first

def resolve_pre_revision_confirmation(
    version: FactVersion,
    context: Context,
    *,
    profile_version: FactVersion | None = None,
    contribution_policy_version: FactVersion | None = None,
    income_tax_policy_version: FactVersion | None = None,
) -> dict:
    """Resolve one evidenced plan or one evidenced full-roster no-change confirmation."""

    fact = version.fact
    scope = _employee_month(fact.employee_id, fact.period)
    plans = tuple(item for kind in PLAN_KINDS for item in context.facts(kind, scope))
    if len(plans) > 1:
        raise KernelError("ambiguous_source", "同一员工和月份存在多个工资方案")
    notices = context.facts(NOTICE_KIND, scope)
    if len(notices) != len({item.subject_id for item in notices}):
        raise KernelError("ambiguous_source", "同一变更通知存在多个有效版本")

    if plans:
        plan = plans[0]
        if type(plan.fact) is not PayrollPlanPreRevision:
            raise NeedsInformation("payroll_plan", "历史工资需要原来的精确月度确认方案")
        if not plan.evidence:
            raise NeedsInformation("payroll_plan.evidence", "负责人确认的工资方案需要留存依据")
        if not _same_payload(plan.fact.payroll, fact):
            raise NeedsInformation("payroll_plan.payroll", "工资事实与负责人确认的精确方案不一致")
        profile_version = profile_version or context.one(PROFILE_KIND, f"@{fact.profile_id}")
        contribution_policy_version = contribution_policy_version or context.one(
            CONTRIBUTION_POLICY_KIND, f"@{fact.contribution_policy_id}"
        )
        _assert_ref(plan.fact.profile_revision, profile_version, "payroll_plan.profile_revision")
        _assert_ref(
            plan.fact.contribution_policy_revision,
            contribution_policy_version,
            "payroll_plan.contribution_policy_revision",
        )
        tax_started = _tax_started(fact, profile_version.fact)
        if tax_started:
            income_tax_policy_version = income_tax_policy_version or context.one(
                INCOME_TAX_POLICY_KIND, f"@{fact.income_tax_policy_id}"
            )
            if plan.fact.income_tax_policy_revision is None:
                raise NeedsInformation(
                    "payroll_plan.income_tax_policy_revision",
                    "工资个税已经开始适用，需要确认精确规则版本",
                )
        elif plan.fact.income_tax_policy_revision is not None:
            raise NeedsInformation(
                "payroll_plan.income_tax_policy_revision",
                "扣缴尚未开始，不能把未采用的个税规则写成确认依据",
            )
        if tax_started:
            assert income_tax_policy_version is not None
            _assert_ref(
                plan.fact.income_tax_policy_revision,
                income_tax_policy_version,
                "payroll_plan.income_tax_policy_revision",
            )
        notice_refs = tuple(sorted((item.subject_id, item.revision) for item in notices))
        confirmed_refs = tuple(
            sorted((item.subject_id, item.revision) for item in plan.fact.change_notice_revisions)
        )
        if notice_refs != confirmed_refs:
            raise NeedsInformation(
                "payroll_plan.change_notice_revisions",
                "工资方案未精确承接本月全部有效变更通知，需要重新确认",
            )
        return {
            "mode": "monthly_plan",
            "confirmation_fact_id": plan.id,
            "confirmation_subject_id": plan.subject_id,
            "confirmation_revision": plan.revision,
            "notice_fact_ids": [item.id for item in sorted(notices, key=lambda item: item.id)],
            "profile_fact_id": profile_version.id,
            "contribution_policy_fact_id": contribution_policy_version.id,
            "income_tax_policy_fact_id": (
                income_tax_policy_version.id if income_tax_policy_version is not None else None
            ),
        }

    raise NeedsInformation("payroll_plan", "历史工资需要原来已留存的精确月度确认方案")


