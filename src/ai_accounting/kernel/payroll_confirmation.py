"""Narrow monthly-wage confirmation resolution shared by prepare and calculate."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Context, FactVersion, KernelError, NeedsInformation, Read
from .types import YearMonth

PLAN_KINDS = ("payroll_plan_v2", "payroll_plan_bounded")
NOTICE_KIND = "payroll_change_notice_v2"
NO_CHANGE_KIND = "payroll_no_change_v2"
PAYROLL_KINDS = ("payroll", "payroll_bounded")
PROFILE_KIND = "payroll_profile"
CONTRIBUTION_POLICY_KIND = "payroll_contribution_policy"
INCOME_TAX_POLICY_KIND = "payroll_income_tax_policy"


class FactRevisionReference(BaseModel):
    """Stable source identity that survives an identity-correction overlay."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    subject_id: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            json_schema_extra={
                "x-accounting-fact": {
                    "role": "management",
                    "meaning": "exact_confirmed_source_subject",
                    "reusable_sources": ["registered_fact_subject"],
                    "constraint": "与revision共同引用负责人确认时已存在的精确事实版本",
                }
            },
        ),
    ]
    revision: Annotated[
        int,
        Field(
            ge=1,
            strict=True,
            json_schema_extra={
                "x-accounting-fact": {
                    "role": "management",
                    "meaning": "exact_confirmed_source_revision",
                    "allowed_precision": ["integer_revision"],
                    "reusable_sources": ["registered_fact_revision"],
                    "constraint": "必须精确匹配正式发布所采用的当前来源版本",
                }
            },
        ),
    ]


def revision_reference(version: FactVersion) -> FactRevisionReference:
    return FactRevisionReference(subject_id=version.subject_id, revision=version.revision)


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


def _one(items, *, field: str, message: str):
    if len(items) > 1:
        raise KernelError("ambiguous_source", message)
    if not items:
        raise NeedsInformation(field, message)
    return items[0]


def _same_payload(left, right) -> bool:
    return left.kind == right.kind and left.model_dump(mode="json") == right.model_dump(mode="json")


def _assert_ref(reference: FactRevisionReference, source: FactVersion, field: str) -> None:
    if revision_reference(source) != reference:
        raise NeedsInformation(field, "工资确认引用的来源版本已变化，需要重新确认")


def _active_profiles(context: Context, period: YearMonth) -> tuple[FactVersion, ...]:
    profiles = tuple(
        item
        for item in context.facts(PROFILE_KIND, "*")
        if item.fact.effective_from <= period
        and (item.fact.effective_to is None or period <= item.fact.effective_to)
    )
    employees = [item.fact.employee_id for item in profiles]
    if len(employees) != len(set(employees)):
        raise KernelError("ambiguous_source", "同一员工存在重叠有效工资档案")
    return profiles


def _tax_started(fact: Any, profile: Any) -> bool:
    first = max(YearMonth(f"{fact.period[:4]}-01"), YearMonth(profile.withholding_start_date[:7]))
    return fact.period >= first


def resolve_payroll_confirmation(
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
        if not plan.evidence:
            raise NeedsInformation("payroll_plan.evidence", "负责人确认的工资方案需要留存依据")
        if not _same_payload(plan.fact.payroll, fact):
            raise NeedsInformation("payroll_plan.payroll", "工资事实与负责人确认的精确方案不一致")
        profile_version = profile_version or context.one(PROFILE_KIND, f"@{fact.profile_id}")
        contribution_policy_version = contribution_policy_version or context.one(
            CONTRIBUTION_POLICY_KIND, f"@{fact.contribution_policy_id}"
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
        _assert_ref(plan.fact.profile_revision, profile_version, "payroll_plan.profile_revision")
        _assert_ref(
            plan.fact.contribution_policy_revision,
            contribution_policy_version,
            "payroll_plan.contribution_policy_revision",
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

    if notices:
        raise NeedsInformation(
            "payroll_plan",
            "本月已有工资变更通知，需要负责人确认本月工资方案",
        )

    confirmations = context.facts(NO_CHANGE_KIND, scope)
    confirmation = _one(
        confirmations,
        field="payroll_no_change",
        message="需要本月工资方案；仅负责人明确全员无变化时可沿用上月",
    )
    if not confirmation.evidence:
        raise NeedsInformation("payroll_no_change.evidence", "全员无变化确认需要留存依据")
    profiles = _active_profiles(context, fact.period)
    roster = tuple(sorted(item.fact.employee_id for item in profiles))
    declared = tuple(sorted(item.employee_id for item in confirmation.fact.employees))
    if roster != declared:
        raise NeedsInformation(
            "payroll_no_change.employees",
            "本月有效员工范围已变化，需要重新确认工资方案或全员无变化",
        )
    entries = [item for item in confirmation.fact.employees if item.employee_id == fact.employee_id]
    if len(entries) != 1:
        raise KernelError("ambiguous_source", "全员无变化确认缺少唯一的当前员工明细")
    entry = entries[0]
    if not _same_payload(entry.payroll_fact(), fact):
        raise NeedsInformation("payroll_no_change.employees.payroll", "沿用的工资输入与确认不一致")

    previous_scope = _employee_month(fact.employee_id, confirmation.fact.prior_period)
    prior_facts = tuple(
        item for kind in PAYROLL_KINDS for item in context.facts(kind, previous_scope)
    )
    prior = _one(prior_facts, field="prior_payroll", message="需要上月唯一的正式工资来源")
    _assert_ref(entry.prior_payroll_revision, prior, "payroll_no_change.prior_payroll_revision")
    prior_calculations = tuple(
        item for kind in PAYROLL_KINDS for item in context.calculations(kind, previous_scope)
    )
    calculation = _one(
        prior_calculations,
        field="prior_payroll",
        message="上期工资尚未发布或待更正，不能沿用",
    )
    if calculation.fact_id != prior.id:
        raise NeedsInformation("prior_payroll", "上期工资尚未发布或待更正，不能沿用")
    if entry.payroll_kind != prior.fact.kind or fact.kind != prior.fact.kind:
        raise NeedsInformation(
            "payroll_no_change.employees.payroll_kind",
            "工资种类与上期不同，须由本月明确方案确认",
        )
    carried_data = prior.fact.model_dump(mode="json") | {"period": str(fact.period)}
    if prior.fact.kind == "payroll_bounded":
        carried_data["tax_income_date"] = None
    carried = type(prior.fact).model_validate(carried_data)
    if not _same_payload(carried, entry.payroll_fact()) or not _same_payload(carried, fact):
        raise NeedsInformation(
            "payroll_no_change.employees.payroll",
            "全员无变化只能原样沿用上月工资输入；金额或扣除变化须由本月方案确认",
        )

    selected_profile = [item for item in profiles if item.fact.employee_id == fact.employee_id]
    profile_version = _one(
        selected_profile, field="payroll_profile", message="本月没有唯一有效的员工工资档案"
    )
    contribution_policy_version = contribution_policy_version or context.one(
        CONTRIBUTION_POLICY_KIND, f"@{fact.contribution_policy_id}"
    )
    tax_started = _tax_started(fact, profile_version.fact)
    if tax_started:
        income_tax_policy_version = income_tax_policy_version or context.one(
            INCOME_TAX_POLICY_KIND, f"@{fact.income_tax_policy_id}"
        )
        if entry.income_tax_policy_revision is None:
            raise NeedsInformation(
                "payroll_no_change.income_tax_policy_revision",
                "工资个税已经开始适用，需要确认精确规则版本",
            )
    elif entry.income_tax_policy_revision is not None:
        raise NeedsInformation(
            "payroll_no_change.income_tax_policy_revision",
            "扣缴尚未开始，不能把未采用的个税规则写成确认依据",
        )
    _assert_ref(entry.profile_revision, profile_version, "payroll_no_change.profile_revision")
    _assert_ref(
        entry.contribution_policy_revision,
        contribution_policy_version,
        "payroll_no_change.contribution_policy_revision",
    )
    if tax_started:
        assert income_tax_policy_version is not None
        _assert_ref(
            entry.income_tax_policy_revision,
            income_tax_policy_version,
            "payroll_no_change.income_tax_policy_revision",
        )
    adopted = set(calculation.values.get("source_versions", ())) | set(
        calculation.values.get("rule_versions", ())
    )
    for source, field in (
        (profile_version, "profile_revision"),
        (contribution_policy_version, "contribution_policy_revision"),
        *(((income_tax_policy_version, "income_tax_policy_revision"),) if tax_started else ()),
    ):
        if source.id not in adopted:
            raise NeedsInformation(
                f"payroll_no_change.{field}",
                "当前员工档案或规则版本与上期工资实际采用版本不同，须确认本月方案",
            )
    return {
        "mode": "explicit_no_change",
        "confirmation_fact_id": confirmation.id,
        "confirmation_subject_id": confirmation.subject_id,
        "confirmation_revision": confirmation.revision,
        "prior_payroll_fact_id": prior.id,
        "profile_fact_id": profile_version.id,
        "contribution_policy_fact_id": contribution_policy_version.id,
        "income_tax_policy_fact_id": (
            income_tax_policy_version.id if income_tax_policy_version is not None else None
        ),
    }
