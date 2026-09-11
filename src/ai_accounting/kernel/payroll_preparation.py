"""Prepare deterministic monthly wage facts; prior amounts require explicit consent.

Preparation does not calculate tax or create payment facts. Existing current
facts and explicit monthly plans take precedence over an unchanged-month basis.
"""

from typing import Annotated, ClassVar, Literal

from pydantic import Field, model_validator

from .contracts import Fact, KernelError, NeedsInformation, Read
from .domains.payroll import PAYROLL_KINDS, Payroll, PayrollBounded, PayrollProfile, employee_month
from .types import YearMonth, digest


class PayrollPlan(Fact):
    kind: ClassVar[str] = "payroll_plan_v2"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("employee_id", "period")
    employee_id: str = Field(min_length=1)
    payroll: Payroll

    def scopes(self):
        return (str(self.period), employee_month(self.employee_id, self.period))

    @model_validator(mode="after")
    def same_employee(self):
        if self.payroll.employee_id != self.employee_id or self.payroll.period != self.period:
            raise ValueError("monthly plan identity differs from wage facts")
        return self


class PayrollChangeNotice(Fact):
    """A known change prevents silent carry-forward until a monthly plan exists."""

    kind: ClassVar[str] = "payroll_change_notice_v2"
    lane: ClassVar[str] = "management"
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


class PayrollNoChange(Fact):
    kind: ClassVar[str] = "payroll_no_change_v2"
    lane: ClassVar[str] = "management"
    prior_period: YearMonth
    basis_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    employee_roster_unchanged: Literal[True]
    salary_and_deductions_unchanged: Literal[True]

    @model_validator(mode="after")
    def consecutive(self):
        if self.prior_period.ordinal != self.period.ordinal - 1:
            raise ValueError("carry-forward requires the immediately preceding month")
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
        changes = self.store.select(connection, Read("fact", PayrollChangeNotice.kind, str(month)))
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
                        "message": "上期工资尚未发布或待更正",
                    }
                )
                continue
            versions.append(calc.id)
            if (
                prior.fact.profile_id != profile.subject_id
                or profile.id not in calc.values["source_versions"]
            ):
                issues.append(
                    {
                        "field": "payroll_profile",
                        "employee_id": employee,
                        "message": "人员扣缴档案已有变化，需要本期明确方案",
                    }
                )
            for kind, subject in (
                ("payroll_contribution_policy", prior.fact.contribution_policy_id),
                ("payroll_income_tax_policy", prior.fact.income_tax_policy_id),
            ):
                if (
                    kind == "payroll_income_tax_policy"
                    and calc.values.get("tax_status") == "not_started"
                ):
                    continue
                policy = self.store.select(connection, Read("fact", kind, "@" + subject))
                versions.extend(item.id for item in policy)
                if len(policy) != 1 or policy[0].id not in calc.values["rule_versions"]:
                    issues.append(
                        {
                            "field": "payroll_policy",
                            "employee_id": employee,
                            "message": "适用规则已有变化，需复核方案",
                        }
                    )
            items.append(
                {
                    "employee_id": employee,
                    "prior_subject_id": prior.subject_id,
                    "prior_fact_id": prior.id,
                    "prior_calculation_id": calc.id,
                    "kind": prior.fact.kind,
                    "data": prior.fact.model_dump(mode="json")
                    | {"period": str(month)}
                    | ({"tax_income_date": None} if isinstance(prior.fact, PayrollBounded) else {}),
                }
            )
        for change in changes:
            versions.append(change.id)
            issues.append(
                {
                    "field": "payroll_change_notice",
                    "employee_id": change.fact.employee_id,
                    "changed_fields": list(change.fact.changed_fields),
                    "message": "已知人员或扣除变化必须由本月明确方案处理",
                }
            )
        result = {
            "period": month,
            "prior_period": previous,
            "employees": items,
            "source_versions": sorted(versions),
            "fact_issues": issues,
        }
        return {**result, "basis_digest": digest(result).hex()}

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
            valid = [
                item
                for item in confirmations
                if item.fact.basis_digest == basis["basis_digest"] and item.evidence
            ]
            existing, candidates, issues = [], [], []
            for profile in profiles:
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
                if saved:
                    existing.append(
                        {
                            "subject_id": saved[0].subject_id,
                            "fact_id": saved[0].id,
                            "employee_id": employee,
                            "source": "current_fact",
                        }
                    )
                    continue
                if planned:
                    source, data = planned[0], planned[0].fact.payroll.model_dump(mode="json")
                    source_kind = "monthly_plan"
                    payroll_kind = planned[0].fact.payroll.kind
                elif valid and not basis["fact_issues"]:
                    source = valid[0]
                    data = next(
                        item["data"]
                        for item in basis["employees"]
                        if item["employee_id"] == employee
                    )
                    source_kind = "explicit_no_change"
                    payroll_kind = next(
                        item["kind"]
                        for item in basis["employees"]
                        if item["employee_id"] == employee
                    )
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
                candidates.append(
                    {
                        "kind": payroll_kind,
                        "subject_id": f"payroll:{employee}:{month}",
                        "data": data,
                        "evidence": list(source.evidence),
                        "expected_revision": 0,
                        "source": source_kind,
                        "source_fact_id": source.id,
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
                    if key not in {"source", "source_fact_id"}
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
