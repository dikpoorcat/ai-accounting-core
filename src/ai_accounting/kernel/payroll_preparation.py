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
    PayrollBounded,
    PayrollContributionPolicy,
    PayrollIncomeTaxPolicy,
    PayrollProfile,
    employee_month,
)
from .payroll_confirmation import (
    FactRevisionReference,
    resolve_payroll_confirmation,
    revision_reference,
)
from .tax_import import assess_tax_import_mapping
from .types import ActualDate, NonNegativeFen, YearMonth, digest


class PayrollPlan(Fact):
    kind: ClassVar[str] = "payroll_plan_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: str = Field(min_length=1)
    payroll: Payroll
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


class PayrollChangeNotice(Fact):
    """A known change prevents silent carry-forward until a monthly plan exists."""

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


class BoundedPayrollPlan(PayrollPlan):
    kind: ClassVar[str] = "payroll_plan_bounded"
    payroll: PayrollBounded


class PayrollReuseInput(BaseModel):
    """Persistable common shape of regular and bounded wage inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    period: YearMonth
    employee_id: Annotated[str, Field(min_length=1, max_length=200)]
    profile_id: Annotated[str, Field(min_length=1, max_length=200)]
    contribution_policy_id: Annotated[str, Field(min_length=1, max_length=200)]
    income_tax_policy_id: Annotated[str, Field(min_length=1, max_length=200)]
    accounting_gross_salary_fen: NonNegativeFen
    tax_reported_salary_fen: NonNegativeFen
    tax_exempt_income_fen: NonNegativeFen | None
    special_additional_deduction_fen: NonNegativeFen | None
    other_legal_deduction_fen: NonNegativeFen | None
    tax_relief_fen: NonNegativeFen | None
    expense_class: Literal["management", "sales", "service"]
    contribution_basis: Literal["policy_until_actual", "actual_required"]
    tax_income_date: ActualDate | None = None


class PayrollReuseEmployee(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    employee_id: Annotated[str, Field(min_length=1, max_length=200)]
    payroll_kind: Literal["payroll", "payroll_bounded"]
    payroll: PayrollReuseInput
    prior_payroll_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "confirmed_prior_payroll_revision",
                "reusable_sources": ["published_prior_payroll"],
                "constraint": "必须是紧邻上月已发布且当前有效的工资事实版本",
            }
        }
    )
    profile_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "unchanged_payroll_profile_revision",
                "reusable_sources": ["payroll_profile"],
                "constraint": "必须与上期工资实际采用的档案精确一致",
            }
        }
    )
    contribution_policy_revision: FactRevisionReference = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "unchanged_contribution_policy_revision",
                "reusable_sources": ["payroll_contribution_policy"],
                "constraint": "必须与上期工资实际采用的规则精确一致",
            }
        }
    )
    income_tax_policy_revision: FactRevisionReference | None = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "unchanged_adopted_income_tax_policy_revision",
                "reusable_sources": ["payroll_income_tax_policy"],
                "constraint": "上期和本期已采用时须精确一致；均未开始扣缴时为空",
            }
        }
    )

    @model_validator(mode="after")
    def consistent(self):
        if self.payroll.employee_id != self.employee_id:
            raise ValueError("no-change employee identity differs from wage input")
        if self.profile_revision.subject_id != self.payroll.profile_id:
            raise ValueError("no-change profile reference differs from wage input")
        if self.contribution_policy_revision.subject_id != self.payroll.contribution_policy_id:
            raise ValueError("no-change contribution policy reference differs from wage input")
        if (
            self.income_tax_policy_revision is not None
            and self.income_tax_policy_revision.subject_id != self.payroll.income_tax_policy_id
        ):
            raise ValueError("no-change income-tax policy reference differs from wage input")
        self.payroll_fact()
        return self

    def payroll_fact(self) -> Payroll | PayrollBounded:
        data = self.payroll.model_dump(mode="json")
        if self.payroll_kind == Payroll.kind:
            data.pop("tax_income_date")
            return Payroll.model_validate(data)
        return PayrollBounded.model_validate(data)


class PayrollNoChange(Fact):
    kind: ClassVar[str] = "payroll_no_change_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("period",)
    prior_period: YearMonth
    employees: Annotated[
        tuple[PayrollReuseEmployee, ...],
        Field(
            min_length=1,
            json_schema_extra={
                "x-accounting-fact": {
                    "role": "management",
                    "meaning": "complete_active_employee_payroll_reuse_basis",
                    "reusable_sources": [
                        "published_prior_payroll",
                        "payroll_profile",
                        "payroll_policy",
                    ],
                    "constraint": "须逐人覆盖本月全部有效员工并保存精确工资输入和来源版本",
                }
            },
        ),
    ]
    employee_roster_unchanged: Annotated[
        Literal[True],
        Field(
            json_schema_extra={
                "x-accounting-fact": {
                    "role": "management",
                    "meaning": "owner_confirmed_complete_roster_unchanged",
                    "reusable_sources": ["owner_confirmation", "employee_roster_materials"],
                    "constraint": "正式发布前持续按本月全部有效员工范围核验",
                }
            }
        ),
    ]
    salary_and_deductions_unchanged: Annotated[
        Literal[True],
        Field(
            json_schema_extra={
                "x-accounting-fact": {
                    "role": "management",
                    "meaning": "owner_confirmed_wage_inputs_unchanged",
                    "reusable_sources": ["owner_confirmation", "published_prior_payroll"],
                    "constraint": "仅允许原样沿用上月工资输入；金额、扣除或种类变化必须另有方案",
                }
            }
        ),
    ]

    def scopes(self):
        return (
            str(self.period),
            *(employee_month(item.employee_id, self.period) for item in self.employees),
        )

    @model_validator(mode="after")
    def consecutive(self):
        if self.prior_period.ordinal != self.period.ordinal - 1:
            raise ValueError("carry-forward requires the immediately preceding month")
        employees = [item.employee_id for item in self.employees]
        if employees != sorted(set(employees)):
            raise ValueError("no-change employees must be unique and sorted")
        if any(item.payroll.period != self.period for item in self.employees):
            raise ValueError("no-change wage inputs must belong to the confirmed month")
        return self


def register(registry):
    for model in (PayrollPlan, BoundedPayrollPlan, PayrollChangeNotice, PayrollNoChange):
        registry.register(model)


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
        employee_ids = [item.fact.employee_id for item in profiles]
        if len(employee_ids) != len(set(employee_ids)):
            issues.append({"field": "payroll_profile", "message": "同一员工存在重叠有效档案"})
        prior_ids = {item.fact.employee_id for item in prior_facts}
        if prior_ids != set(employee_ids):
            issues.append(
                {"field": "employee_roster", "message": "人员范围与上期不同，须明确本期人员和工资"}
            )
        for profile in sorted(profiles, key=lambda item: item.fact.employee_id):
            employee = profile.fact.employee_id
            matching = [item for item in prior_facts if item.fact.employee_id == employee]
            versions.append(profile.id)
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
            if prior.fact.profile_id != profile.subject_id:
                issues.append(
                    {
                        "field": "payroll_profile",
                        "employee_id": employee,
                        "message": "上期工资引用的员工档案与本月有效档案不同，需要本期明确方案",
                    }
                )
            tax_started = month >= max(
                YearMonth(f"{month[:4]}-01"), YearMonth(profile.fact.withholding_start_date[:7])
            )
            policies = []
            policy_sources = [
                (PayrollContributionPolicy.kind, prior.fact.contribution_policy_id),
                *(
                    [(PayrollIncomeTaxPolicy.kind, prior.fact.income_tax_policy_id)]
                    if tax_started
                    else []
                ),
            ]
            for kind, subject in policy_sources:
                selected = self.store.select(connection, Read("fact", kind, "@" + subject))
                versions.extend(item.id for item in selected)
                if len(selected) != 1:
                    issues.append(
                        {
                            "field": "payroll_policy",
                            "employee_id": employee,
                            "message": "工资沿用需要唯一的当前规则版本",
                        }
                    )
                    policies = []
                    break
                policies.append(selected[0])
            if len(policies) != len(policy_sources) or prior.fact.profile_id != profile.subject_id:
                continue
            adopted = set(calc.values.get("source_versions", ())) | set(
                calc.values.get("rule_versions", ())
            )
            if any(item.id not in adopted for item in (profile, *policies)):
                issues.append(
                    {
                        "field": "payroll_source_revision",
                        "employee_id": employee,
                        "message": (
                            "本月员工档案或规则版本与上期工资实际采用版本不同，需要本月明确方案"
                        ),
                    }
                )
                continue
            payroll_type = PayrollBounded if isinstance(prior.fact, PayrollBounded) else Payroll
            payroll = payroll_type.model_validate(
                prior.fact.model_dump(mode="json")
                | {"period": str(month)}
                | ({"tax_income_date": None} if isinstance(prior.fact, PayrollBounded) else {})
            )
            reuse = PayrollReuseEmployee(
                employee_id=employee,
                payroll_kind=payroll.kind,
                payroll=payroll.model_dump(mode="json"),
                prior_payroll_revision=revision_reference(prior),
                profile_revision=revision_reference(profile),
                contribution_policy_revision=revision_reference(policies[0]),
                income_tax_policy_revision=(
                    revision_reference(policies[1]) if tax_started else None
                ),
            )
            items.append(reuse.model_dump(mode="json"))
        result = {
            "period": month,
            "prior_period": previous,
            "employees": items,
            "source_versions": sorted(versions),
            "fact_issues": issues,
        }
        return result

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
            plans = [
                item
                for kind in (PayrollPlan.kind, BoundedPayrollPlan.kind)
                for item in self.store.select(connection, Read("fact", kind, period))
            ]
            confirmations = self.store.select(
                connection, Read("fact", PayrollNoChange.kind, period)
            )
            existing, candidates, issues = [], [], []
            if len(confirmations) > 1:
                issues.append(
                    {"field": "payroll_no_change", "message": "本月存在多个全员无变化确认"}
                )
            for profile in sorted(profiles, key=lambda item: item.fact.employee_id):
                employee = profile.fact.employee_id
                saved = [item for item in current if item.fact.employee_id == employee]
                planned = [item for item in plans if item.fact.employee_id == employee]
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
                    source, data = planned[0], planned[0].fact.payroll.model_dump(mode="json")
                    source_kind = "monthly_plan"
                    payroll_kind = planned[0].fact.payroll.kind
                elif len(confirmations) == 1:
                    source = confirmations[0]
                    pending_prior = [
                        item
                        for item in basis["fact_issues"]
                        if item.get("employee_id") == employee
                        and item.get("code") == "pending_publication"
                    ]
                    if pending_prior:
                        issues.extend(pending_prior)
                        continue
                    entries = [
                        item for item in source.fact.employees if item.employee_id == employee
                    ]
                    if len(entries) != 1:
                        issues.append(
                            {
                                "field": "payroll_no_change.employees",
                                "employee_id": employee,
                                "message": "全员无变化确认缺少唯一的员工工资输入",
                            }
                        )
                        continue
                    data = entries[0].payroll_fact().model_dump(mode="json")
                    source_kind = "explicit_no_change"
                    payroll_kind = entries[0].payroll_kind
                else:
                    issues.append(
                        {
                            "field": "payroll_plan",
                            "employee_id": employee,
                            "message": "需要本月工资方案；仅明确无变化且来源仍有效时可沿用上月",
                            "reusable_sources": ["payroll_plan_v2", "payroll_no_change_v2"],
                        }
                    )
                    continue

                payroll_type = PayrollBounded if payroll_kind == PayrollBounded.kind else Payroll
                target = payroll_type.model_validate(data)
                if (
                    saved
                    and planned
                    and (
                        saved[0].fact.kind != target.kind
                        or saved[0].fact.employee_id != target.employee_id
                        or saved[0].fact.period != target.period
                    )
                ):
                    issues.append(
                        {
                            "field": "payroll_plan.payroll",
                            "employee_id": employee,
                            "message": "工资种类、员工或月份变更须通过身份纠错处理",
                        }
                    )
                    continue
                target_version = FactVersion(
                    id=(saved[0].id if saved else f"prepared:{target.kind}:{employee}:{month}"),
                    subject_id=(saved[0].subject_id if saved else f"payroll:{employee}:{month}"),
                    revision=(saved[0].revision if saved else 1),
                    fact=target,
                    evidence=tuple(source.evidence),
                )
                selections = self.store.select_many(connection, target.reads())
                context = Context(selections)
                try:
                    confirmation = resolve_payroll_confirmation(target_version, context)
                except NeedsInformation as exc:
                    issues.extend(
                        item | {"employee_id": employee} for item in exc.response()["fact_issues"]
                    )
                    continue
                except KernelError as exc:
                    if exc.code != "ambiguous_source":
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

                if saved and saved[0].fact.model_dump(mode="json") == data:
                    existing.append(
                        {
                            "subject_id": saved[0].subject_id,
                            "fact_id": saved[0].id,
                            "employee_id": employee,
                            "source": source_kind,
                            "source_fact_id": source.id,
                            "payroll_confirmation": confirmation,
                        }
                    )
                    continue
                if saved and not planned:
                    issues.append(
                        {
                            "field": "payroll_no_change.employees.payroll",
                            "employee_id": employee,
                            "message": "已有工资与全员无变化确认不一致，需要明确本月工资方案",
                        }
                    )
                    continue
                candidates.append(
                    {
                        "kind": payroll_kind,
                        "subject_id": (
                            saved[0].subject_id if saved else f"payroll:{employee}:{month}"
                        ),
                        "data": data,
                        "evidence": list(source.evidence),
                        "expected_revision": saved[0].revision if saved else 0,
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
                "tax_import_mapping": assess_tax_import_mapping(
                    self.store, connection, YearMonth(month)
                ),
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
            raise NeedsInformation("payroll_plan", "工资准备尚缺明确来源")
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
                "source_fact_ids": [item["source_fact_id"] for item in plan["candidates"]],
                "publish_subjects": [item["subject_id"] for item in plan["candidates"]],
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
