"""Narrow monthly-wage confirmation resolution shared by prepare and calculate."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Context, FactVersion, KernelError, NeedsInformation, Read
from .types import YearMonth

PLAN_KINDS = ("payroll_plan_v2",)
NOTICE_KIND = "payroll_change_notice_v2"
NO_CHANGE_KIND = "payroll_no_change_v2"
PAYROLL_KINDS = ("payroll", "payroll_bounded")
PROFILE_KIND = "payroll_profile"
CONTRIBUTION_POLICY_KIND = "payroll_contribution_policy"


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


def identity_confirmation_companion(store, connection, before, after, changes) -> bool:
    """Allow only exact approvals accompanying this batch's wage identity changes."""
    from .types import canonical

    if before.kind not in {*PLAN_KINDS, NO_CHANGE_KIND} or before.kind != after.kind:
        return False
    by_subject = {item.subject_id: item for item in changes}

    def changed_wage(old_ref, new_ref):
        change = by_subject.get(old_ref.subject_id)
        if (
            change is None
            or change.action != "reassign"
            or change.data is None
            or old_ref.subject_id != new_ref.subject_id
            or old_ref.revision != change.expected_revision
            or new_ref.revision != old_ref.revision + 1
        ):
            return None
        saved = store.current_fact(connection, change.subject_id)
        if saved.fact.kind not in PAYROLL_KINDS or saved.revision != old_ref.revision:
            return None
        wage = type(saved.fact).model_validate_json(canonical(change.data))
        excluded = {"employee_id", "profile_revision"}
        if saved.fact.employee_id == wage.employee_id or saved.fact.model_dump(
            exclude=excluded
        ) != wage.model_dump(exclude=excluded):
            return None
        profile = store.current_fact(connection, wage.profile_revision.subject_id)
        if (
            profile.fact.kind != PROFILE_KIND
            or revision_reference(profile) != wage.profile_revision
            or profile.fact.employee_id != wage.employee_id
        ):
            return None
        return wage.employee_id

    if before.kind in PLAN_KINDS:
        if before.model_dump(
            exclude={"payroll_revision", "change_notice_revisions"}
        ) != after.model_dump(exclude={"payroll_revision", "change_notice_revisions"}):
            return False
        employee = changed_wage(before.payroll_revision, after.payroll_revision)
        if employee is None:
            return False
        old_notices = {item.subject_id: item for item in before.change_notice_revisions}
        new_notices = {item.subject_id: item for item in after.change_notice_revisions}
        if old_notices.keys() != new_notices.keys():
            return False
        for subject, old_ref in old_notices.items():
            new_ref = new_notices[subject]
            if old_ref == new_ref:
                continue
            change = by_subject.get(subject)
            if (
                change is None
                or change.action != "reassign"
                or change.data is None
                or old_ref.revision != change.expected_revision
                or new_ref.revision != old_ref.revision + 1
            ):
                return False
            saved = store.current_fact(connection, subject)
            if saved.fact.kind != NOTICE_KIND or saved.revision != old_ref.revision:
                return False
            notice = type(saved.fact).model_validate_json(canonical(change.data))
            if (
                notice.employee_id != employee
                or notice.employee_id == saved.fact.employee_id
                or notice.model_dump(exclude={"employee_id"})
                != saved.fact.model_dump(exclude={"employee_id"})
            ):
                return False
        return True

    if before.model_dump(exclude={"employees"}) != after.model_dump(exclude={"employees"}):
        return False
    old_refs = {
        item.prior_payroll_revision.subject_id: item.prior_payroll_revision
        for item in before.employees
    }
    new_refs = {
        item.prior_payroll_revision.subject_id: item.prior_payroll_revision
        for item in after.employees
    }
    if old_refs.keys() != new_refs.keys():
        return False
    changed = False
    for subject, old_ref in old_refs.items():
        if old_ref == new_refs[subject]:
            continue
        if changed_wage(old_ref, new_refs[subject]) is None:
            return False
        changed = True
    return changed


def _previous(period: YearMonth) -> YearMonth:
    return YearMonth.from_ordinal(period.ordinal - 1)


def _employee_month(employee_id: str, period: YearMonth) -> str:
    return f"employee:{employee_id}:month:{period}"


def confirmation_reads(fact: Any, subject_id: str | None = None) -> tuple[Read, ...]:
    """Declare every input the confirmation decision may select, including an empty roster."""

    scope = _employee_month(fact.employee_id, fact.period)
    previous_scope = str(_previous(fact.period))
    return tuple(
        dict.fromkeys(
            (
                *(
                    Read(
                        "fact",
                        kind,
                        "payroll-confirmation:" + subject_id
                        if subject_id is not None
                        else str(fact.period),
                    )
                    for kind in PLAN_KINDS
                ),
                Read("fact", NOTICE_KIND, scope),
                Read("fact", NO_CHANGE_KIND, str(fact.period)),
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


def payroll_policy_source(context: Context, fact: Any, field: str, kind: str) -> FactVersion:
    """The wage owns its exact policy reference; current changes invalidate it."""
    reference = getattr(fact, field)
    if reference is None:
        raise NeedsInformation(field, "本期核算需要明确的精确工资规则版本")
    source = context.one(kind, "@" + reference.subject_id)
    _assert_ref(reference, source, field)
    if kind == PROFILE_KIND and (
        source.fact.employee_id != fact.employee_id
        or source.fact.effective_from > fact.period
        or source.fact.effective_to is not None
        and source.fact.effective_to < fact.period
    ):
        raise KernelError("payroll_profile_mismatch", "精确工资档案必须属于本员工并覆盖本月")
    return source


def carried_payroll(prior: FactVersion, period: YearMonth):
    data = prior.fact.model_dump(mode="json") | {"period": str(period)}
    return type(prior.fact).model_validate(data)


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


def resolve_payroll_confirmation(
    version: FactVersion,
    context: Context,
    *,
    profile_version: FactVersion | None = None,
    contribution_policy_version: FactVersion | None = None,
) -> dict:
    """Resolve one evidenced plan or one evidenced full-roster no-change confirmation."""

    fact = version.fact
    scope = _employee_month(fact.employee_id, fact.period)
    plan_scope = "payroll-confirmation:" + version.subject_id
    plans = tuple(item for kind in PLAN_KINDS for item in context.facts(kind, plan_scope))
    if len(plans) > 1:
        raise KernelError("ambiguous_source", "同一员工和月份存在多个工资方案")
    notices = context.facts(NOTICE_KIND, scope)
    if len(notices) != len({item.subject_id for item in notices}):
        raise KernelError("ambiguous_source", "同一变更通知存在多个有效版本")

    if plans:
        plan = plans[0]
        if not plan.evidence:
            raise NeedsInformation("payroll_plan.evidence", "负责人确认的工资方案需要留存依据")
        if plan.fact.period != fact.period:
            raise NeedsInformation("payroll_plan.period", "工资确认期间与工资事实不一致")
        _assert_ref(plan.fact.payroll_revision, version, "payroll_plan.payroll_revision")
        profile_version = profile_version or payroll_policy_source(
            context, fact, "profile_revision", PROFILE_KIND
        )
        contribution_policy_version = contribution_policy_version or payroll_policy_source(
            context, fact, "contribution_policy_revision", CONTRIBUTION_POLICY_KIND
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
        }

    if notices:
        raise NeedsInformation(
            "payroll_plan",
            "本月已有工资变更通知，需要负责人确认本月工资方案",
        )

    confirmations = context.facts(NO_CHANGE_KIND, str(fact.period))
    confirmation = _one(
        confirmations,
        field="payroll_no_change",
        message="需要本月工资方案；仅负责人明确全员无变化时可沿用上月",
    )
    if not confirmation.evidence:
        raise NeedsInformation("payroll_no_change.evidence", "全员无变化确认需要留存依据")
    profiles = _active_profiles(context, fact.period)
    roster = tuple(sorted(item.fact.employee_id for item in profiles))
    previous_scope = str(_previous(fact.period))
    prior_facts = tuple(
        item for kind in PAYROLL_KINDS for item in context.facts(kind, previous_scope)
    )
    prior_roster = tuple(sorted(item.fact.employee_id for item in prior_facts))
    if prior_roster != roster:
        raise NeedsInformation(
            "payroll_no_change.employees",
            "本月有效员工范围与上期完整工资人员范围不同，需要明确本月工资方案",
        )
    prior_calculations = tuple(
        item for kind in PAYROLL_KINDS for item in context.calculations(kind, previous_scope)
    )
    selected = []
    for entry in confirmation.fact.employees:
        prior = _one(
            [
                item
                for item in prior_facts
                if item.subject_id == entry.prior_payroll_revision.subject_id
            ],
            field="prior_payroll",
            message="需要上月唯一的工资来源",
        )
        _assert_ref(entry.prior_payroll_revision, prior, "payroll_no_change.prior_payroll_revision")
        calculation = _one(
            [item for item in prior_calculations if item.subject_id == prior.subject_id],
            field="prior_payroll",
            message="上期工资尚未发布或待更正，不能沿用",
        )
        if calculation.fact_id != prior.id:
            raise NeedsInformation("prior_payroll", "上期工资尚未发布或待更正，不能沿用")
        selected.append((prior, calculation))
    declared = tuple(sorted(item.fact.employee_id for item, _ in selected))
    if roster != declared:
        raise NeedsInformation(
            "payroll_no_change.employees",
            "本月有效员工范围已变化，需要重新确认工资方案或全员无变化",
        )
    entries = [
        (prior, calculation)
        for prior, calculation in selected
        if prior.fact.employee_id == fact.employee_id
    ]
    if len(entries) != 1:
        raise KernelError("ambiguous_source", "全员无变化确认缺少唯一的当前员工明细")
    prior, calculation = entries[0]
    if fact.kind != prior.fact.kind:
        raise NeedsInformation(
            "payroll_no_change.employees.payroll_kind",
            "工资种类与上期不同，须由本月明确方案确认",
        )
    carried = carried_payroll(prior, fact.period)
    if not _same_payload(carried, fact):
        raise NeedsInformation(
            "payroll_no_change.employees.payroll",
            "全员无变化只能原样沿用上月工资输入；金额或扣除变化须由本月方案确认",
        )

    selected_profile = [item for item in profiles if item.fact.employee_id == fact.employee_id]
    profile_version = _one(
        selected_profile, field="payroll_profile", message="本月没有唯一有效的员工工资档案"
    )
    _assert_ref(fact.profile_revision, profile_version, "profile_revision")
    contribution_policy_version = contribution_policy_version or payroll_policy_source(
        context, fact, "contribution_policy_revision", CONTRIBUTION_POLICY_KIND
    )
    adopted = set(calculation.values.get("source_versions", ())) | set(
        calculation.values.get("rule_versions", ())
    )
    for source, field in (
        (profile_version, "profile_revision"),
        (contribution_policy_version, "contribution_policy_revision"),
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
    }
