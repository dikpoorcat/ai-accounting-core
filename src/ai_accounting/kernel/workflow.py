"""Evidence-backed external work; a completion binds the exact published basis.

Deadlines and applicability are confirmed facts, never inferred from an empty
database. Recalculation cannot rewrite the fact that an external filing occurred.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .contracts import Context, Fact, KernelError, NeedsInformation, Outcome, Read
from .domains.payroll import PAYROLL_KINDS
from .periods import Periods
from .types import ActualDate, YearMonth, digest

ObligationKind = Literal[
    "contribution_declaration",
    "individual_income_tax",
    "quarterly_tax_and_reports",
    "annual_income_tax",
    "annual_business_report",
]
SOURCES = {
    "contribution_declaration": PAYROLL_KINDS,
    "individual_income_tax": (
        *PAYROLL_KINDS,
        "annual_bonus",
        "labor",
        "labor_accrual",
        "labor_project_cost",
    ),
    # Quarter completion requires prior close, but accepts the explicitly selected
    # current calculations. These can differ from the old frozen close manifest
    # after a later correction; basis_current reports that distinction honestly.
    "quarterly_tax_and_reports": ("*",),
    "annual_income_tax": ("income_tax_assessment",),
    "annual_business_report": (),
}
MONTHLY_PAYROLL_OBLIGATIONS = {"contribution_declaration", "individual_income_tax"}
NON_ACCOUNTING_CALCULATIONS = {"external_completion", "payroll_disbursement_basis"}
EXTERNAL_WORKFLOW_KINDS = frozenset(NON_ACCOUNTING_CALCULATIONS)


class FilingRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    obligation_kind: ObligationKind
    cycle: Literal["monthly", "quarterly", "annual"]


class FilingDeadline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    obligation_kind: ObligationKind
    end_period: YearMonth
    due_date: ActualDate


class FilingCalendarPolicy(Fact):
    """Explicit scope/deadline rules, not an inferred tax filing calendar."""

    kind: ClassVar[str] = "filing_calendar_policy_v2"
    lane: ClassVar[str] = "management"
    version: str = Field(min_length=1)
    effective_from: YearMonth
    effective_to: YearMonth
    primary_source_url: Annotated[str, Field(pattern=r"^https://[^/\s]+/[^\s]*$")]
    rules: tuple[FilingRule, ...]
    deadlines: tuple[FilingDeadline, ...] = ()

    @model_validator(mode="after")
    def rule_identity(self):
        if self.effective_from > self.effective_to:
            raise ValueError("invalid calendar validity")
        kinds = [rule.obligation_kind for rule in self.rules]
        keys = [(item.obligation_kind, item.end_period) for item in self.deadlines]
        if len(kinds) != len(set(kinds)) or len(keys) != len(set(keys)):
            raise ValueError("duplicate filing rule or deadline")
        if any(item.obligation_kind not in kinds for item in self.deadlines):
            raise ValueError("deadline requires an explicit rule")
        return self


class CompanyWorkflowScope(Fact):
    kind: ClassVar[str] = "company_workflow_scope_v2"
    lane: ClassVar[str] = "management"
    established_period: YearMonth
    effective_from: YearMonth
    effective_to: YearMonth
    calendar_policy_id: str = Field(min_length=1)
    applicability: dict[ObligationKind, Literal["required", "not_applicable"]]

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.established_period > self.effective_from or self.effective_from > self.effective_to:
            raise ValueError("invalid company coverage")
        if set(self.applicability) != set(SOURCES):
            raise ValueError("each external work category needs explicit applicability")
        return self


def _basis_reads(kind, start, end):
    return (
        *(
            Read(source, source_kind, str(YearMonth.from_ordinal(month)))
            for month in range(start.ordinal, end.ordinal + 1)
            for source_kind in SOURCES[kind]
            for source in (("calculation",) if source_kind == "*" else ("fact", "calculation"))
        ),
        *((Read("fact", "payroll_profile", "*"),) if kind in MONTHLY_PAYROLL_OBLIGATIONS else ()),
    )


class ExternalObligation(Fact):
    kind: ClassVar[str] = "external_obligation"
    lane: ClassVar[str] = "management"
    identity_fields: ClassVar[tuple[str, ...]] = ("obligation_kind", "start_period", "end_period")
    obligation_kind: ObligationKind
    start_period: YearMonth
    end_period: YearMonth
    due_date: ActualDate | None = Field(
        default=None,
        description="已建立的外部办理截止日；管理期限可未知，不据此推定到期",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "external_deadline",
                "precision": "day",
            }
        },
    )
    applicability_confirmed: Literal[True]
    applicability: Literal["required", "not_applicable"] = "required"

    @model_validator(mode="after")
    def interval(self):
        if not 0 <= self.end_period.ordinal - self.start_period.ordinal <= 11:
            raise ValueError("an external obligation covers one to twelve months")
        if self.due_date is not None and self.due_date.period < self.end_period:
            raise ValueError("deadline cannot precede the covered interval")
        return self

    def scopes(self):
        return (str(self.period), f"obligation:{self.obligation_kind}:{self.end_period}")

    def basis_reads(self):
        return _basis_reads(self.obligation_kind, self.start_period, self.end_period)


class AcceptedCalculation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    subject_id: str = Field(min_length=1)
    calculation_id: str = Field(min_length=1)


class ExternalCompletion(Fact):
    kind: ClassVar[str] = "external_completion"
    lane: ClassVar[str] = "management"
    immutable: ClassVar[bool] = True
    identity_fields: ClassVar[tuple[str, ...]] = ("obligation_id",)
    obligation_id: str = Field(min_length=1)
    obligation_fact_id: str = Field(min_length=1)
    # Retained range makes all calculator reads explicit before loading context.
    obligation_kind: ObligationKind
    start_period: YearMonth
    end_period: YearMonth
    accepted_calculations: tuple[AcceptedCalculation, ...]
    completion_status: Literal["submitted", "confirmed_complete"]
    date_status: Literal["known", "not_established"]
    completion_date: ActualDate | None = Field(
        default=None, description="可选的实际外部完成日；未建立时保存为空，不以记录月份补造"
    )
    no_reportable_activity_confirmed: StrictBool | None = None

    @model_validator(mode="after")
    def dates(self):
        if not 0 <= self.end_period.ordinal - self.start_period.ordinal <= 11:
            raise ValueError("invalid obligation interval")
        if (self.date_status == "known") != (self.completion_date is not None):
            raise ValueError("a historical attestation does not invent a completion date")
        if self.completion_date and self.completion_date.period > self.period:
            raise ValueError("completion has not occurred in the recorded period")
        if self.obligation_kind == "quarterly_tax_and_reports" and self.period <= self.end_period:
            raise ValueError("record quarterly completion after its covered closing months")
        return self

    def scopes(self):
        return (str(self.period), "completion:" + self.obligation_id)

    def reads(self):
        return (
            Read("fact", "external_obligation", "@" + self.obligation_id),
            Read("fact", "external_obligation", "#" + self.obligation_fact_id),
            *(
                Read("calculation", "*", "#" + item.calculation_id)
                for item in self.accepted_calculations
            ),
            *_basis_reads(self.obligation_kind, self.start_period, self.end_period),
        )

    def required_closed_periods(self):
        if self.obligation_kind != "quarterly_tax_and_reports":
            return ()
        return tuple(
            YearMonth.from_ordinal(month)
            for month in range(self.start_period.ordinal, self.end_period.ordinal + 1)
        )


def calculate_completion(version, context):
    fact = version.fact
    if not version.evidence:
        raise NeedsInformation("completion_evidence", "需要真实外部提交或完成凭据")
    obligation = context.one("external_obligation", "@" + fact.obligation_id)
    if any(
        getattr(fact, name) != getattr(obligation.fact, name)
        for name in ("obligation_kind", "start_period", "end_period")
    ):
        raise KernelError("obligation_changed", "外部义务的已确认范围发生变化")
    accepted = _accepted_history(fact, context.select)
    basis, issues = _basis_state(obligation.fact, context.select)
    if issues:
        raise NeedsInformation(
            "accepted_calculations",
            "外部申报依据存在未发布或未衔接的新业务",
            sources=tuple(item["subject_id"] for item in issues),
        )
    if (
        not basis
        and SOURCES[fact.obligation_kind]
        and fact.no_reportable_activity_confirmed is not True
    ):
        raise NeedsInformation("no_reportable_activity_confirmed", "空计算集合不能证明无申报业务")
    return Outcome(
        (),
        {
            "obligation_id": fact.obligation_id,
            "obligation_kind": fact.obligation_kind,
            "completion_status": fact.completion_status,
            "date_status": fact.date_status,
            "completion_date": fact.completion_date,
            "basis_current": _result_basis(accepted) == _result_basis(basis)
            and obligation.fact.applicability == "required",
            "accepted_calculations": [item.model_dump() for item in fact.accepted_calculations],
            "reviewed_calculations": _reviewed_basis(basis),
            "obligation_fact_id": fact.obligation_fact_id,
            "completion_evidence": list(version.evidence),
        },
    )


def _accepted_history(fact, select):
    """Validate immutable submission references without substituting current heads."""
    if len({item.subject_id for item in fact.accepted_calculations}) != len(
        fact.accepted_calculations
    ):
        raise KernelError("duplicate_basis", "申报采用的计算版本不能重复")
    originals = select(Read("fact", "external_obligation", "#" + fact.obligation_fact_id))
    if len(originals) != 1:
        raise NeedsInformation("obligation_fact_id", "需要确实存在的原义务事实版本")
    original = originals[0]
    if (
        original.subject_id != fact.obligation_id
        or original.fact.applicability != "required"
        or any(
            getattr(fact, name) != getattr(original.fact, name)
            for name in ("obligation_kind", "start_period", "end_period")
        )
    ):
        raise KernelError("invalid_obligation_history", "原义务版本不属于本次提交的事项和范围")
    accepted = []
    for reference in fact.accepted_calculations:
        items = select(Read("calculation", "*", "#" + reference.calculation_id))
        if len(items) != 1:
            raise NeedsInformation("accepted_calculations", "需要确实存在的已接受正式计算版本")
        item = items[0]
        if (
            item.subject_id != reference.subject_id
            or item.kind in NON_ACCOUNTING_CALCULATIONS
            or not fact.start_period <= item.period <= fact.end_period
            or (
                "*" not in SOURCES[fact.obligation_kind]
                and item.kind not in SOURCES[fact.obligation_kind]
            )
            or not item.result_digest
            or (
                fact.obligation_kind == "individual_income_tax"
                and item.kind in PAYROLL_KINDS
                and item.values.get("tax_status") == "not_started"
            )
        ):
            raise KernelError("invalid_accepted_calculation", "接受版本的业务身份、类型或月份不符")
        accepted.append(item)
    return tuple(accepted)


def _result_basis(calculations):
    return {(item.subject_id, item.kind, item.period, item.result_digest) for item in calculations}


def _reviewed_basis(calculations):
    """The latest published review is distinct from the original external submission."""
    return [
        {
            "subject_id": item.subject_id,
            "calculation_id": item.id,
            "kind": item.kind,
            "period": item.period,
            "result_digest": item.result_digest,
        }
        for item in sorted(calculations, key=lambda item: item.subject_id)
    ]


def _basis_state(obligation, select):
    """Known source facts and current results must describe the same population."""
    facts, calculations, profiles = {}, {}, {}
    for read in obligation.basis_reads():
        for item in select(read):
            if read.source == "calculation" and item.kind in NON_ACCOUNTING_CALCULATIONS:
                continue
            collection = (
                profiles
                if read.kind == "payroll_profile"
                else (facts if read.source == "fact" else calculations)
            )
            collection[item.subject_id] = item
    issues = _payroll_population_issues(
        profiles.values(), facts.values(), obligation.start_period, obligation.end_period
    )
    if "*" not in SOURCES[obligation.obligation_kind]:
        for subject in sorted(facts.keys() | calculations.keys()):
            fact, calc = facts.get(subject), calculations.get(subject)
            if fact is None or calc is None or calc.fact_id != fact.id:
                issues.append(
                    {
                        "field": "unpublished_basis",
                        "subject_id": subject,
                        "message": "当前业务事实尚未形成对应的正式计算",
                    }
                )
    return tuple(
        calculations[subject]
        for subject in sorted(calculations)
        if not (
            obligation.obligation_kind == "individual_income_tax"
            and calculations[subject].kind in PAYROLL_KINDS
            and calculations[subject].values.get("tax_status") == "not_started"
        )
    ), issues


def _payroll_population_issues(profiles, facts, start, end):
    issues = []
    payroll_months = {
        (item.fact.employee_id, item.fact.period)
        for item in facts
        if item.fact.kind in PAYROLL_KINDS
    }
    missing = set()
    for version in profiles:
        profile = version.fact
        for ordinal in range(start.ordinal, end.ordinal + 1):
            month = YearMonth.from_ordinal(ordinal)
            identity = profile.employee_id, month
            if (
                profile.effective_from <= month
                and (profile.effective_to is None or month <= profile.effective_to)
                and identity not in payroll_months
                and identity not in missing
            ):
                missing.add(identity)
                issues.append(
                    {
                        "field": "missing_payroll",
                        "subject_id": version.subject_id,
                        "employee_id": profile.employee_id,
                        "period": month,
                        "domain": "payroll",
                        "message": "有效工资档案对应月份尚未建立工资事实",
                    }
                )
    return issues


def payroll_required_reads(period):
    return (
        Read("fact", "payroll_profile", "*"),
        *(
            Read(source, kind, str(period))
            for kind in PAYROLL_KINDS
            for source in ("fact", "calculation")
        ),
    )


def payroll_required_work(period, context):
    facts = tuple(item for kind in PAYROLL_KINDS for item in context.facts(kind, str(period)))
    calculations = {
        item.subject_id: item
        for kind in PAYROLL_KINDS
        for item in context.calculations(kind, str(period))
    }
    issues = _payroll_population_issues(
        context.facts("payroll_profile", "*"), facts, period, period
    )
    issues.extend(
        {
            "field": "unpublished_payroll",
            "subject_id": item.subject_id,
            "domain": "payroll",
            "message": "工资事实尚未形成对应的正式计算",
        }
        for item in facts
        if item.subject_id not in calculations or calculations[item.subject_id].fact_id != item.id
    )
    return issues


def _matches_basis(obligation_version, completion_fact, completion, basis, issues):
    if issues or completion.fact_id != completion_fact.id or not completion_fact.evidence:
        return False
    fact = completion_fact.fact
    if obligation_version.fact.applicability != "required" or any(
        getattr(fact, name) != getattr(obligation_version.fact, name)
        for name in ("obligation_kind", "start_period", "end_period")
    ):
        return False
    if (
        not basis
        and SOURCES[fact.obligation_kind]
        and fact.no_reportable_activity_confirmed is not True
    ):
        return False
    # A stored flag alone is insufficient: a new current version must pass the
    # evaluator and the common publisher before its review can be relied on.
    return completion.values["basis_current"] and tuple(
        completion.values["reviewed_calculations"]
    ) == tuple(_reviewed_basis(basis))


def _completion_confirmation_times(connection, fact_ids):
    """Read immutable confirmation metadata without adding it to business calculation.

    A saved fact has one confirmation audit, including when saved in a batch or
    with an actor envelope. Replays reuse that fact and amendments create a new
    fact ID. Walk the audit primary key backwards and stop once all are found.
    """
    missing, found = set(fact_ids), {}
    if not missing:
        return found
    rows = connection.execute(
        "SELECT action,payload,created_at FROM audit WHERE action IN "
        "('confirm_fact','confirm_facts','recording_correction') ORDER BY id DESC"
    )
    try:
        for row in rows:
            payload = json.loads(row["payload"])
            result = payload.get("result", payload)
            entries = result.get("results", ()) if row["action"] == "confirm_facts" else (result,)
            for entry in entries:
                fact_id = entry.get("fact_id")
                if fact_id in missing:
                    found[fact_id] = row["created_at"]
                    missing.remove(fact_id)
            if not missing:
                break
    finally:
        rows.close()
    return found


def _completion_known_as_of(completion_date, confirmed_at, as_of):
    if completion_date is not None:
        return completion_date <= as_of
    if confirmed_at is None:
        return False
    try:
        confirmed = datetime.fromisoformat(confirmed_at)
    except ValueError:
        return False
    if confirmed.tzinfo is None:
        return False
    # as_of is a Chinese accounting calendar day (inclusive), not an instant.
    # Keep its boundary stable across restore hosts; never use OS local time.
    confirmed_day = confirmed.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    return confirmed_day <= as_of


def register(registry):
    registry.register(CompanyWorkflowScope)
    registry.register(FilingCalendarPolicy)
    registry.register(ExternalObligation)
    registry.register(ExternalCompletion, calculate_completion)
    registry.register_readiness("payroll_presence", payroll_required_reads, payroll_required_work)


def required_reads(period):
    return (
        Read("fact", "external_obligation", "*"),
        Read("fact", "external_completion", "*"),
        Read("calculation", "external_completion", "*"),
        Read("fact", "payroll_profile", "*"),
        *(
            Read(source, kind, "*")
            for kind in SOURCES["individual_income_tax"]
            for source in ("fact", "calculation")
        ),
    )


def required_work(period, context):
    obligations = context.facts("external_obligation", "*")
    completion_facts = {item.subject_id: item for item in context.facts("external_completion", "*")}
    completions = context.calculations("external_completion", "*")
    issues = []
    for version in obligations:
        fact = version.fact
        if fact.applicability != "required":
            continue
        if fact.obligation_kind not in MONTHLY_PAYROLL_OBLIGATIONS:
            continue
        if fact.end_period > period:
            continue
        matching = [
            item for item in completions if item.values["obligation_id"] == version.subject_id
        ]

        def select(read):
            if read.key == "*":
                return context.select(read)
            return tuple(
                item
                for item in context.select(Read(read.source, read.kind, "*"))
                if (item.fact.period if read.source == "fact" else item.period) == read.key
            )

        basis, basis_issues = _basis_state(fact, select)
        if not any(
            item.subject_id in completion_facts
            and _matches_basis(
                version,
                completion_facts[item.subject_id],
                item,
                basis,
                basis_issues,
            )
            for item in matching
        ):
            issues.append(
                {
                    "field": "external_declaration",
                    "obligation_id": version.subject_id,
                    "message": "已确认的申报义务缺少与当前结果一致的完成依据",
                }
            )
    return issues


class Workflow:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def prepare_obligations(self, period: str):
        """Return current-month and current quarter/year facts for explicit confirmation.

        A missing official deadline stays unknown. Existing manual declarations
        are reused by meaning; conflicting scope is returned for review.
        """
        month = YearMonth(period)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            scopes = [
                item
                for item in self.store.select(
                    connection, Read("fact", CompanyWorkflowScope.kind, "*")
                )
                if item.fact.effective_from <= month <= item.fact.effective_to
            ]
            if len(scopes) != 1:
                raise NeedsInformation(
                    "company_workflow_scope", "需要唯一覆盖本月的明确公司适用范围"
                )
            scope = scopes[0]
            policies = self.store.select(
                connection,
                Read("fact", FilingCalendarPolicy.kind, "@" + scope.fact.calendar_policy_id),
            )
            if (
                len(policies) != 1
                or not policies[0].fact.effective_from <= month <= policies[0].fact.effective_to
            ):
                raise NeedsInformation("filing_calendar_policy", "需要覆盖本月的版本化官方规则")
            policy = policies[0]
            rules = {rule.obligation_kind: rule for rule in policy.fact.rules}
            if set(rules) != set(SOURCES):
                raise NeedsInformation(
                    "filing_calendar_policy.rules", "需明确全部五类事项的周期规则"
                )
            deadlines = {
                (item.obligation_kind, item.end_period): item.due_date
                for item in policy.fact.deadlines
            }
            existing = self.store.select(connection, Read("fact", ExternalObligation.kind, "*"))
            candidates, reused, issues = [], [], []
            for kind in SOURCES:
                width = {"monthly": 1, "quarterly": 3, "annual": 12}[rules[kind].cycle]
                start = YearMonth.from_ordinal(month.ordinal - (int(month[5:]) - 1) % width)
                end = YearMonth.from_ordinal(start.ordinal + width - 1)
                start = max(start, scope.fact.established_period)
                if (
                    start < scope.fact.effective_from
                    or end > scope.fact.effective_to
                    or start < policy.fact.effective_from
                    or end > policy.fact.effective_to
                ):
                    issues.append(
                        {
                            "field": "coverage",
                            "obligation_kind": kind,
                            "message": "公司适用范围与规则须覆盖整个申报周期，不能推定其余月份",
                        }
                    )
                    continue
                data = ExternalObligation(
                    period=month,
                    obligation_kind=kind,
                    start_period=start,
                    end_period=end,
                    due_date=deadlines.get((kind, end)),
                    applicability_confirmed=True,
                    applicability=scope.fact.applicability[kind],
                ).model_dump(mode="json")
                matching = [
                    item
                    for item in existing
                    if item.fact.obligation_kind == kind
                    and item.fact.start_period == start
                    and item.fact.end_period == end
                ]
                if matching:
                    if (
                        len(matching) != 1
                        or matching[0].fact.applicability != data["applicability"]
                        or matching[0].fact.due_date != data["due_date"]
                    ):
                        issues.append(
                            {
                                "field": "external_obligation",
                                "obligation_kind": kind,
                                "message": "已有义务与当前明确范围或期限冲突，需显式复核",
                            }
                        )
                    else:
                        reused.append(matching[0].subject_id)
                    continue
                candidates.append(
                    {
                        "kind": ExternalObligation.kind,
                        "subject_id": f"external:{kind}:{start}:{end}",
                        "data": data,
                        "evidence": sorted(set(scope.evidence + policy.evidence)),
                        "expected_revision": 0,
                    }
                )
            result = {
                "period": month,
                "candidates": candidates,
                "reused": reused,
                "fact_issues": issues,
                "source_fact_ids": [scope.id, policy.id],
                "policy_version": policy.fact.version,
                "primary_source_url": policy.fact.primary_source_url,
                "epochs": {"management": self.store.epochs(connection)["management"]},
            }
            return {
                **result,
                "digest": digest(result).hex(),
                "status": "needs_information" if issues else "ready",
            }

    def confirm_obligations(self, period: str, *, preview_digest: str, request_id: str):
        request_hash = digest(["workflow_obligations", period, preview_digest])
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        plan = self.prepare_obligations(period)
        if plan["digest"] != preview_digest:
            raise KernelError("preview_expired", "公司范围或规则已变化，请重新准备")
        if plan["fact_issues"]:
            raise KernelError(
                "needs_information", "申报周期事实尚不完整", fact_issues=plan["fact_issues"]
            )
        registrations = [self.engine._registration(False, **item)[1] for item in plan["candidates"]]

        def operation(connection):
            return {
                "status": "confirmed",
                "source_fact_ids": plan["source_fact_ids"],
                "results": [save(connection) for save in registrations],
                "reused": plan["reused"],
            }

        return self.engine._write(
            request_id,
            request_hash,
            plan["epochs"],
            ("management",),
            "workflow_obligations",
            operation,
        )

    def obligation_basis(self, obligation_id):
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            version = self.store.current_fact(connection, obligation_id)
            if not isinstance(version.fact, ExternalObligation):
                raise KernelError("invalid_obligation", "需要类型化外部义务")
            fact = version.fact
            if fact.applicability != "required":
                raise KernelError("obligation_not_applicable", "明确不适用的事项无需申报依据")
            selections = {read: self.store.select(connection, read) for read in fact.basis_reads()}
            pending = {
                row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM pending")
            }
            basis, issues = _basis_state(fact, selections.__getitem__)
            if issues:
                raise KernelError(
                    "basis_unpublished",
                    "已确认来源尚未全部发布，不能用空依据确认完成",
                    fact_issues=issues,
                )
            if pending.intersection(item.subject_id for item in basis):
                raise KernelError("basis_pending", "申报依据尚有待更正事项")
            if fact.obligation_kind == "quarterly_tax_and_reports":
                closed = {
                    row[0]
                    for row in connection.execute(
                        "SELECT period FROM period_close WHERE period BETWEEN ? AND ?",
                        (fact.start_period.ordinal, fact.end_period.ordinal),
                    )
                }
                if closed != set(range(fact.start_period.ordinal, fact.end_period.ordinal + 1)):
                    raise KernelError("awaiting_close", "季度税费及报表准备等待覆盖月份关账")
            return {
                "obligation_id": obligation_id,
                "obligation_fact_id": version.id,
                "obligation_kind": fact.obligation_kind,
                "start_period": fact.start_period,
                "end_period": fact.end_period,
                "accepted_calculations": [
                    {"subject_id": item.subject_id, "calculation_id": item.id}
                    for item in sorted(basis, key=lambda value: value.subject_id)
                ],
            }

    def query(self, period: str, *, as_of: str):
        month, day = YearMonth(period), ActualDate(as_of)
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            closed = connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (month.ordinal,)
            ).fetchone()
            _, issues, unpublished = Periods.completeness(
                connection, month.ordinal, self.store.registry
            )
            pending = {
                row[0] for row in connection.execute("SELECT DISTINCT subject_id FROM pending")
            }
            facts = self.store.select(connection, Read("fact", "external_obligation", "*"))
            completion_calculations = self.store.select(
                connection, Read("calculation", "external_completion", "*")
            )
            confirmation_times = _completion_confirmation_times(
                connection,
                (
                    item.fact_id
                    for item in completion_calculations
                    if item.values["completion_date"] is None
                ),
            )
            obligations = []
            for version in facts:
                fact = version.fact
                basis, basis_issues = _basis_state(
                    fact,
                    lambda read: self.store.select(connection, read),
                )
                if pending.intersection(item.subject_id for item in basis):
                    basis_issues.append({"field": "basis_pending", "message": "核算依据待更正"})
                completions = self.store.select(
                    connection,
                    Read("calculation", "external_completion", "completion:" + version.subject_id),
                )
                completion_facts = {
                    item.subject_id: item
                    for item in self.store.select(
                        connection,
                        Read("fact", "external_completion", "completion:" + version.subject_id),
                    )
                }
                matching = [
                    item
                    for item in completions
                    if item.subject_id not in pending
                    and item.subject_id in completion_facts
                    and _matches_basis(
                        version, completion_facts[item.subject_id], item, basis, basis_issues
                    )
                ]
                known_as_of = {
                    item.id: _completion_known_as_of(
                        item.values["completion_date"], confirmation_times.get(item.fact_id), day
                    )
                    for item in completions
                }
                current = [item for item in matching if known_as_of[item.id]]
                status = (
                    "not_applicable"
                    if fact.applicability == "not_applicable"
                    else (
                        "completed"
                        if current
                        else (
                            "due"
                            if fact.due_date is not None and fact.due_date <= day
                            else "pending"
                        )
                    )
                )
                if (
                    closed
                    and fact.obligation_kind in MONTHLY_PAYROLL_OBLIGATIONS
                    and fact.end_period <= month
                ):
                    status = "closed"
                obligations.append(
                    {
                        "id": version.subject_id,
                        "kind": fact.obligation_kind,
                        "start_period": fact.start_period,
                        "end_period": fact.end_period,
                        "due_date": fact.due_date,
                        "status": status,
                        "basis_review_required": any(known_as_of.values()) and not current,
                        "completion_calculations": sorted(item.id for item in current),
                        "basis_issues": basis_issues,
                        "recorded_completions": [
                            {
                                "calculation_id": item.id,
                                "status": item.values["completion_status"],
                                "completion_date": item.values["completion_date"],
                                "basis_current": item in matching,
                                "known_as_of": known_as_of[item.id],
                                "confirmation_recorded_at": confirmation_times.get(item.fact_id),
                            }
                            for item in completions
                        ],
                    }
                )
            readiness_issues = []
            if not closed:
                for required_reads, evaluate in self.store.registry.readiness.values():
                    context = Context(
                        {
                            read: self.store.select(connection, read)
                            for read in required_reads(month)
                        }
                    )
                    readiness_issues.extend(evaluate(month, context))
            labels = (
                "银行流水",
                "员工及工资变动",
                "社保及公积金",
                "个人所得税",
                "票据及非银行业务",
                "关账确认",
            )
            categories = ("bank", "payroll", "payroll", "payroll", "transactions", None)
            steps = []
            for number, (label, category) in enumerate(zip(labels, categories, strict=True), 1):
                related = [
                    issue
                    for issue in issues
                    if category and issue["field"] in {"materials", f"materials.{category}"}
                ]
                if number in {1, 2, 5}:
                    relevant_kinds = {
                        kind
                        for kind, model in self.store.registry.models.items()
                        if model.material_category == category
                    }
                    related.extend(
                        {"field": row["id"], "message": "已确认业务尚未核算"}
                        for row in unpublished
                        if row["kind"] in relevant_kinds
                        and row["kind"] in self.store.registry.evaluators
                    )
                    for kind in relevant_kinds:
                        for row in self.store.select(connection, Read("fact", kind, str(month))):
                            if row.subject_id in pending:
                                related.append({"field": row.subject_id, "message": "核算待更正"})
                if number == 2:
                    related.extend(
                        issue for issue in readiness_issues if issue.get("domain") == "payroll"
                    )
                declaration_kind = {3: "contribution_declaration", 4: "individual_income_tax"}.get(
                    number
                )
                not_applicable = False
                if declaration_kind:
                    matching = [
                        item
                        for item in obligations
                        if item["kind"] == declaration_kind
                        and item["start_period"] <= month <= item["end_period"]
                    ]
                    not_applicable = bool(matching) and all(
                        item["status"] == "not_applicable" for item in matching
                    )
                    if not matching:
                        related.append(
                            {
                                "field": "external_obligation",
                                "message": "需要明确申报适用范围或不适用事实",
                            }
                        )
                    elif any(
                        item["status"] not in {"completed", "not_applicable", "closed"}
                        for item in matching
                    ):
                        related.append(
                            {
                                "field": "external_completion",
                                "message": "需要与当前核算依据一致的真实申报完成凭据",
                            }
                        )
                if number == 6:
                    related = [
                        *issues,
                        *readiness_issues,
                        *(
                            {"field": row["id"], "message": "尚未核算"}
                            for row in unpublished
                            if row["kind"] in self.store.registry.evaluators
                        ),
                    ]
                steps.append(
                    {
                        "number": number,
                        "label": label,
                        "status": "closed"
                        if closed
                        else (
                            "needs_information"
                            if related
                            else (
                                "not_applicable"
                                if not_applicable
                                else ("completed" if declaration_kind else "ready")
                            )
                        ),
                        "fact_issues": [] if closed else related,
                    }
                )
            for number, kind in (
                (7, "quarterly_tax_and_reports"),
                (8, "annual_income_tax"),
                (9, "annual_business_report"),
            ):
                due = [
                    item
                    for item in obligations
                    if item["kind"] == kind
                    and item["status"] not in {"completed", "not_applicable"}
                ]
                if due:
                    steps.append({"number": number, "obligations": due})
            return {
                "period": period,
                "as_of": day,
                "steps": steps,
                "obligations": obligations,
                "fact_issues": [] if closed else readiness_issues,
            }
