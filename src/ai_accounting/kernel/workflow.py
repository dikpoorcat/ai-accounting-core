"""Evidence-backed external work; a completion binds the exact published basis.

Deadlines and applicability are confirmed facts, never inferred from an empty
database. Recalculation cannot rewrite the fact that an external filing occurred.
"""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .contracts import Context, Fact, KernelError, NeedsInformation, Outcome, Read
from .periods import Periods
from .types import ActualDate, YearMonth

ObligationKind = Literal[
    "contribution_declaration",
    "individual_income_tax",
    "quarterly_tax_and_reports",
    "annual_income_tax",
    "annual_business_report",
]
SOURCES = {
    "contribution_declaration": ("payroll",),
    "individual_income_tax": ("payroll", "annual_bonus", "labor"),
    # Quarter completion requires prior close, but accepts the explicitly selected
    # current calculations. These can differ from the old frozen close manifest
    # after a later correction; basis_current reports that distinction honestly.
    "quarterly_tax_and_reports": ("*",),
    "annual_income_tax": ("income_tax_assessment",),
    "annual_business_report": (),
}
MONTHLY_PAYROLL_OBLIGATIONS = {"contribution_declaration", "individual_income_tax"}
NON_ACCOUNTING_CALCULATIONS = {"external_completion"}


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
    return tuple(calculations[subject] for subject in sorted(calculations)), issues


def _payroll_population_issues(profiles, facts, start, end):
    issues = []
    payroll_months = {
        (item.fact.employee_id, item.fact.period) for item in facts if item.fact.kind == "payroll"
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
        Read("fact", "payroll", str(period)),
        Read("calculation", "payroll", str(period)),
    )


def payroll_required_work(period, context):
    facts = context.facts("payroll", str(period))
    calculations = {item.subject_id: item for item in context.calculations("payroll", str(period))}
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


def register(registry):
    registry.register(ExternalObligation)
    registry.register(ExternalCompletion, calculate_completion)
    registry.register_readiness("external_monthly_declarations", required_reads, required_work)
    registry.register_readiness("payroll_presence", payroll_required_reads, payroll_required_work)


def required_reads(period):
    return (
        Read("fact", "external_obligation", "*"),
        Read("fact", "external_completion", "*"),
        Read("calculation", "external_completion", "*"),
        Read("fact", "payroll_profile", "*"),
        *(
            Read(source, kind, "*")
            for kind in ("payroll", "annual_bonus", "labor")
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
                current = [
                    item
                    for item in completions
                    if item.subject_id not in pending
                    and item.subject_id in completion_facts
                    and _matches_basis(
                        version, completion_facts[item.subject_id], item, basis, basis_issues
                    )
                    and (
                        completion_facts[item.subject_id].fact.completion_date <= day
                        if completion_facts[item.subject_id].fact.completion_date is not None
                        else completion_facts[item.subject_id].fact.period < day.period
                    )
                ]
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
                obligations.append(
                    {
                        "id": version.subject_id,
                        "kind": fact.obligation_kind,
                        "start_period": fact.start_period,
                        "end_period": fact.end_period,
                        "due_date": fact.due_date,
                        "status": status,
                        "completion_calculations": sorted(item.id for item in current),
                        "basis_issues": basis_issues,
                        "recorded_completions": [
                            {
                                "calculation_id": item.id,
                                "status": item.values["completion_status"],
                                "completion_date": item.values["completion_date"],
                                "basis_current": item in current,
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
                    if category and issue["field"] == f"materials.{category}"
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
                        item["status"] not in {"completed", "not_applicable"} for item in matching
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
                        if closed and number == 6
                        else (
                            "needs_information"
                            if related
                            else (
                                "not_applicable"
                                if not_applicable
                                else ("completed" if declaration_kind else "ready")
                            )
                        ),
                        "fact_issues": related,
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
