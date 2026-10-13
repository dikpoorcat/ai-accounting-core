"""Prepare deterministic monthly wage facts; prior amounts require explicit consent.

Preparation does not calculate tax or create payment facts. Existing current
facts and explicit monthly plans take precedence over an unchanged-month basis.
"""

from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Context, Fact, FactVersion, KernelError, NeedsInformation, Read
from .domains.payroll import (
    PAYROLL_KINDS,
    Payroll,
    PayrollContributionPolicy,
    PayrollProfile,
    employee_month,
)
from .payroll_confirmation import (
    FactRevisionReference,
    carried_payroll,
    resolve_payroll_confirmation,
    revision_reference,
)
from .tax_import import assess_tax_import_mapping
from .types import YearMonth, digest


class PayrollPlan(Fact):
    """An evidenced confirmation references one wage revision without copying it."""

    kind: ClassVar[str] = "payroll_plan_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("period",)
    payroll_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "owner_confirmed_exact_wage_revision",
                "reusable_sources": ["registered_payroll", "registered_payroll_bounded"],
                "constraint": "引用已登记工资精确版本；正文和政策采用只保存于工资事实",
            }
        }
    )
    change_notice_revisions: tuple[FactRevisionReference, ...] = Field(
        default=(),
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "owner_confirmed_change_notice_versions",
                "reusable_sources": ["payroll_change_notice_v2"],
            }
        },
    )

    def scopes(self):
        return (str(self.period), "payroll-confirmation:" + self.payroll_revision.subject_id)

    @model_validator(mode="after")
    def ordered_notices(self):
        refs = [(item.subject_id, item.revision) for item in self.change_notice_revisions]
        if refs != sorted(set(refs)):
            raise ValueError("confirmed notices must be unique and sorted")
        return self


class PayrollChangeNotice(Fact):
    kind: ClassVar[str] = "payroll_change_notice_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: str = Field(min_length=1)
    changed_fields: Annotated[
        tuple[
            Literal[
                "employment",
                "salary",
                "special_additional_deduction",
                "other_legal_deduction",
                "tax_exempt_income",
                "tax_relief",
                "contribution_profile",
            ],
            ...,
        ],
        Field(min_length=1),
    ]

    def scopes(self):
        return (str(self.period), employee_month(self.employee_id, self.period))


class PayrollReuseEmployee(BaseModel):
    """One exact prior wage input, whose current formal adoption is checked anew."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    prior_payroll_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "owner_confirmed_exact_prior_wage_input",
                "reusable_sources": ["published_prior_payroll", "published_prior_payroll_bounded"],
                "constraint": "上期工资版本须仍是当前正式采用版本；正文与政策由该工资事实取得",
            }
        }
    )


class PayrollNoChange(Fact):
    kind: ClassVar[str] = "payroll_no_change_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("period",)
    employees: Annotated[tuple[PayrollReuseEmployee, ...], Field(min_length=1)]
    employee_roster_unchanged: Literal[True] = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "owner_confirmed_complete_roster_unchanged",
                "reusable_sources": ["owner_confirmation"],
            }
        }
    )
    salary_and_deductions_unchanged: Literal[True] = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "owner_confirmed_wage_and_deductions_unchanged",
                "reusable_sources": ["owner_confirmation"],
            }
        }
    )

    @model_validator(mode="after")
    def unique_sources(self):
        identities = [item.prior_payroll_revision.subject_id for item in self.employees]
        if identities != sorted(set(identities)):
            raise ValueError("prior wage references must be unique and sorted")
        return self


def register(registry):
    from .source24_history import PayrollPlanPreRevision

    for model in (PayrollPlan, PayrollChangeNotice, PayrollNoChange):
        registry.register(
            model,
            historical=((PayrollPlanPreRevision, "fact_payroll_plan_v2_pre_revision"),)
            if model is PayrollPlan else (),
        )


class PayrollPreparation:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _basis(self, connection, month):
        previous = YearMonth.from_ordinal(month.ordinal - 1)
        profiles = [
            item
            for item in self.store.select(connection, Read("fact", PayrollProfile.kind, "*"))
            if item.fact.effective_from <= month
            and (item.fact.effective_to is None or month <= item.fact.effective_to)
        ]
        prior_facts = [
            item
            for kind in PAYROLL_KINDS
            for item in self.store.select(connection, Read("fact", kind, str(previous)))
        ]
        prior_calcs = {
            item.subject_id: item
            for kind in PAYROLL_KINDS
            for item in self.store.select(connection, Read("calculation", kind, str(previous)))
        }
        pending = {row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM pending")}
        items, issues, versions = [], [], []
        roster = [item.fact.employee_id for item in profiles]
        if len(roster) != len(set(roster)):
            issues.append({"field": "payroll_profile", "message": "同一员工存在重叠有效档案"})
        if {item.fact.employee_id for item in prior_facts} != set(roster):
            issues.append(
                {"field": "employee_roster", "message": "人员范围与上期不同，须明确本期人员和工资"}
            )
        for profile in sorted(profiles, key=lambda item: item.fact.employee_id):
            employee = profile.fact.employee_id
            versions.append(profile.id)
            matching = [item for item in prior_facts if item.fact.employee_id == employee]
            if len(matching) != 1:
                issues.append(
                    {
                        "field": "prior_payroll",
                        "employee_id": employee,
                        "message": "需要上一个月唯一的正式工资或本月明确方案",
                    }
                )
                continue
            prior = matching[0]
            if not isinstance(prior.fact, Payroll):
                issues.append(
                    {
                        "field": "remuneration_revision",
                        "employee_id": employee,
                        "message": "历史工资须明确修订为当前来源确认格式，不能自动沿用旧计税正文",
                        "reusable_sources": [prior.subject_id],
                    }
                )
                continue
            calc = prior_calcs.get(prior.subject_id)
            versions.append(prior.id)
            if calc is None or calc.fact_id != prior.id or prior.subject_id in pending:
                issues.append(
                    {
                        "field": "prior_payroll",
                        "employee_id": employee,
                        "code": "pending_publication",
                        "message": "上期工资尚未发布或待更正",
                    }
                )
                continue
            versions.append(calc.id)
            adopted = set(calc.values.get("source_versions", ())) | set(
                calc.values.get("rule_versions", ())
            )
            sources = [
                ("profile_revision", PayrollProfile.kind),
                ("contribution_policy_revision", PayrollContributionPolicy.kind),
            ]
            problem = False
            for field, kind in sources:
                reference = getattr(prior.fact, field)
                selected = (
                    self.store.select(connection, Read("fact", kind, "@" + reference.subject_id))
                    if reference is not None
                    else ()
                )
                versions.extend(item.id for item in selected)
                if (
                    len(selected) != 1
                    or revision_reference(selected[0]) != reference
                    or selected[0].id not in adopted
                    or field == "profile_revision"
                    and selected[0].id != profile.id
                ):
                    issues.append(
                        {
                            "field": "payroll_source_revision",
                            "employee_id": employee,
                            "message": "档案或规则版本不同于上期实际采用，需要本月明确方案",
                        }
                    )
                    problem = True
            if not problem:
                items.append(
                    PayrollReuseEmployee(
                        prior_payroll_revision=revision_reference(prior)
                    ).model_dump(mode="json")
                )
        items.sort(key=lambda item: item["prior_payroll_revision"]["subject_id"])
        return {
            "period": month,
            "prior_period": previous,
            "employees": items,
            "source_versions": sorted(set(versions)),
            "fact_issues": issues,
        }

    def reuse_basis(self, period: str):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return self._basis(connection, YearMonth(period))

    def prepare(self, period: str):
        month = YearMonth(period)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            basis = self._basis(connection, month)
            profiles = [
                item
                for item in self.store.select(connection, Read("fact", PayrollProfile.kind, "*"))
                if item.fact.effective_from <= month
                and (item.fact.effective_to is None or month <= item.fact.effective_to)
            ]
            current = [
                item
                for kind in PAYROLL_KINDS
                for item in self.store.select(connection, Read("fact", kind, period))
            ]
            plans = self.store.select(connection, Read("fact", PayrollPlan.kind, period))
            confirmations = self.store.select(
                connection, Read("fact", PayrollNoChange.kind, period)
            )
            existing, candidates, issues = [], [], []
            if len(confirmations) > 1:
                issues.append(
                    {"field": "payroll_no_change", "message": "本月存在多个全员无变化确认"}
                )
            from .source24_history import PayrollPlanPreRevision, PayrollPreRevision

            def plan_matches(plan, wage):
                if type(plan.fact) is PayrollPlanPreRevision:
                    return (type(wage.fact) is PayrollPreRevision
                            and plan.fact.payroll == wage.fact)
                return plan.fact.payroll_revision.subject_id == wage.subject_id

            for plan in plans:
                wages = [w for w in current if plan_matches(plan, w)]
                wage = wages[0] if len(wages) == 1 else None
                if wage is None:
                    issues.append(
                        {
                            "field": "payroll_plan.payroll_revision",
                            "message": "确认须引用本月已登记的精确工资版本",
                        }
                    )
            for profile in sorted(profiles, key=lambda item: item.fact.employee_id):
                employee = profile.fact.employee_id
                saved = [item for item in current if item.fact.employee_id == employee]
                planned = [
                    item
                    for item in plans
                    if any(plan_matches(item, wage) for wage in saved)
                ]
                if len(saved) > 1 or len(planned) > 1:
                    issues.append(
                        {
                            "field": "payroll",
                            "employee_id": employee,
                            "message": "人员本月存在重复工资或方案",
                        }
                    )
                    continue
                if planned:
                    source, version = planned[0], saved[0]
                    if not isinstance(version.fact, (Payroll, PayrollPreRevision)):
                        issues.append(
                            {
                                "field": "remuneration_revision",
                                "employee_id": employee,
                                "message": "历史工资须明确修订为当前来源确认格式",
                                "reusable_sources": [version.subject_id],
                            }
                        )
                        continue
                    source_kind, target = "monthly_plan", version.fact
                elif len(confirmations) == 1:
                    source = confirmations[0]
                    if basis["fact_issues"]:
                        issues.extend(basis["fact_issues"])
                        continue
                    previous = str(YearMonth.from_ordinal(month.ordinal - 1))
                    prior_facts = [
                        item
                        for kind in PAYROLL_KINDS
                        for item in self.store.select(connection, Read("fact", kind, previous))
                    ]
                    referenced = {
                        entry.prior_payroll_revision.subject_id for entry in source.fact.employees
                    }
                    prior = [
                        item
                        for item in prior_facts
                        if item.fact.employee_id == employee and item.subject_id in referenced
                    ]
                    if len(prior) != 1:
                        issues.append(
                            {
                                "field": "payroll_no_change.employees",
                                "employee_id": employee,
                                "message": "全员无变化确认缺少唯一的上期工资引用",
                            }
                        )
                        continue
                    target = carried_payroll(prior[0], month)
                    source_kind = "explicit_no_change"
                    version = (
                        saved[0]
                        if saved
                        else FactVersion(
                            f"prepared:{target.kind}:{employee}:{month}",
                            f"payroll:{employee}:{month}",
                            1,
                            target,
                            tuple(source.evidence),
                        )
                    )
                else:
                    issues.append(
                        {
                            "field": "payroll_plan",
                            "employee_id": employee,
                            "message": "先登记并确认本月工资；仅明确全员无变化时可沿用上月",
                            "reusable_sources": ["payroll_plan_v2", "payroll_no_change_v2"],
                        }
                    )
                    continue
                selections = self.store.select_many(
                    connection, version.fact.reads_for(version.subject_id)
                )
                try:
                    confirmation = resolve_payroll_confirmation(version, Context(selections))
                except NeedsInformation as exc:
                    issues.extend(
                        {**item, "employee_id": employee}
                        for item in exc.issues_with_context()
                    )
                    continue
                except KernelError as exc:
                    if exc.code not in {"ambiguous_source", "payroll_profile_mismatch"}:
                        raise
                    issues.append(
                        {
                            "field": "payroll_confirmation",
                            "employee_id": employee,
                            "message": str(exc),
                            "code": exc.code,
                        }
                    )
                    continue
                if saved:
                    existing.append(
                        {
                            "subject_id": version.subject_id,
                            "fact_id": version.id,
                            "employee_id": employee,
                            "source": source_kind,
                            "source_fact_id": source.id,
                            "payroll_confirmation": confirmation,
                        }
                    )
                else:
                    candidates.append(
                        {
                            "kind": target.kind,
                            "subject_id": version.subject_id,
                            "data": target.model_dump(mode="json"),
                            "evidence": list(source.evidence),
                            "expected_revision": 0,
                            "source": source_kind,
                            "source_fact_id": source.id,
                            "payroll_confirmation": confirmation,
                        }
                    )
            if not profiles:
                issues.append(
                    {
                        "field": "payroll_profile",
                        "message": "没有覆盖本月的明确人员范围，不能据此推定零工资",
                    }
                )
            if len({item.fact.employee_id for item in profiles}) != len(profiles):
                issues.append({"field": "payroll_profile", "message": "有效人员档案范围重叠"})
            result = {
                "period": month,
                "tax_import_mapping": assess_tax_import_mapping(self.store, connection, month),
                "existing": existing,
                "candidates": candidates,
                "fact_issues": issues,
                "reuse_basis": basis,
                "epochs": {
                    key: value
                    for key, value in self.store.epochs(connection).items()
                    if key in {"accounting", "management"}
                },
            }
            return {
                **result,
                "status": "needs_information" if issues else "ready",
                "digest": digest(result).hex(),
            }

    def confirm(self, period: str, *, preview_digest: str, request_id: str):
        request_hash = digest(["prepare_payroll", period, preview_digest])
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        plan = self.prepare(period)
        if plan["digest"] != preview_digest:
            raise KernelError("preview_expired", "人员、工资或来源已变化，请重新准备")
        if plan["fact_issues"]:
            raise NeedsInformation(plan["fact_issues"])
        registrations = [
            self.engine._registration(
                False,
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {"source", "source_fact_id", "payroll_confirmation"}
                },
            )[1]
            for item in plan["candidates"]
        ]

        def operation(connection):
            return {
                "status": "confirmed",
                "results": [save(connection) for save in registrations],
                "existing": plan["existing"],
                "source_fact_ids": sorted(
                    {item["source_fact_id"] for item in (*plan["candidates"], *plan["existing"])}
                ),
                "publish_subjects": [
                    item["subject_id"] for item in (*plan["candidates"], *plan["existing"])
                ],
            }

        return self.engine._write(
            request_id,
            request_hash,
            plan["epochs"],
            ("accounting",),
            "prepare_payroll",
            operation,
            checked_lanes=("accounting", "management"),
        )
