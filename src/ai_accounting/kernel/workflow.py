"""Evidence-backed external work; a completion binds the exact published basis.

Deadlines and applicability are confirmed facts, never inferred from an empty
database. Recalculation cannot rewrite the fact that an external filing occurred.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ai_accounting.policy_sources import OfficialPolicySourceURL

from .contracts import Fact, KernelError, NeedsInformation, Outcome, Read
from .domains.payroll import PAYROLL_KINDS
from .provenance import recorded_times
from .types import ActualDate, YearMonth, digest, sum_fen


@dataclass(frozen=True)
class ObligationDefinition:
    basis_kinds: tuple[str, ...]
    check_payroll_population: bool = False
    exclude_not_started: bool = False


OBLIGATION_DEFINITIONS = MappingProxyType(
    {
        "contribution_declaration": ObligationDefinition(
            PAYROLL_KINDS,
            check_payroll_population=True,
        ),
        "individual_income_tax": ObligationDefinition(
            (
                *PAYROLL_KINDS,
                "annual_bonus",
                "labor",
                "labor_accrual",
                "labor_project_cost",
            ),
            check_payroll_population=True,
            exclude_not_started=True,
        ),
        "quarterly_tax": ObligationDefinition(("tax_assessment",)),
        "quarterly_financial_report": ObligationDefinition(("*",)),
        "annual_income_tax": ObligationDefinition(("income_tax_assessment",)),
        "annual_business_report": ObligationDefinition(()),
    }
)
LABELS = MappingProxyType(
    {
        "contribution_declaration": "社保申报",
        "individual_income_tax": "个人所得税申报",
        "quarterly_tax": "季度税务申报",
        "quarterly_financial_report": "季度财务报表报送",
        "annual_income_tax": "年度企业所得税申报",
        "annual_business_report": "年度工商报告",
    }
)
ObligationKind = Literal[*OBLIGATION_DEFINITIONS]
SOURCES = MappingProxyType(
    {kind: definition.basis_kinds for kind, definition in OBLIGATION_DEFINITIONS.items()}
)
MONTHLY_PAYROLL_OBLIGATIONS = frozenset(
    kind
    for kind, definition in OBLIGATION_DEFINITIONS.items()
    if definition.check_payroll_population
)
NON_ACCOUNTING_CALCULATIONS = {
    "external_completion",
    "external_basis_review",
    "payroll_disbursement_basis",
}
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
    primary_source_url: OfficialPolicySourceURL
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
        if set(self.applicability) != set(OBLIGATION_DEFINITIONS):
            raise ValueError("each external work category needs explicit applicability")
        return self


def _basis_reads(kind, start, end):
    definition = OBLIGATION_DEFINITIONS[kind]
    return (
        *(
            Read(source, source_kind, str(YearMonth.from_ordinal(month)))
            for month in range(start.ordinal, end.ordinal + 1)
            for source_kind in definition.basis_kinds
            for source in (("calculation",) if source_kind == "*" else ("fact", "calculation"))
        ),
        *((Read("fact", "payroll_profile", "*"),) if definition.check_payroll_population else ()),
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


class AdoptedSourceFact(BaseModel):
    """An exact observed source used in the real external submission."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    subject_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)


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
    source_facts: tuple[AdoptedSourceFact, ...] = ()
    accepted_calculations: tuple[AcceptedCalculation, ...] = ()
    adopted_evidence_digests: tuple[str, ...] = Field(
        default=(),
        description="原提交资料的不可变凭据摘要；须包含在本次事实的证据中",
    )
    previous_completion_fact_id: str | None = None
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
        if self.obligation_kind == "quarterly_financial_report" and self.period <= self.end_period:
            raise ValueError("record quarterly completion after its covered closing months")
        if (
            not self.source_facts
            and not self.accepted_calculations
            and not self.adopted_evidence_digests
            and self.no_reportable_activity_confirmed is not True
        ):
            raise ValueError("actual submission needs adopted sources or explicit no activity")
        return self

    def scopes(self):
        return (str(self.period), "completion:" + self.obligation_id)

    def reads(self):
        return (
            Read("fact", "external_obligation", "@" + self.obligation_id),
            Read("fact", "external_obligation", "#" + self.obligation_fact_id),
            Read("fact", "external_completion", "completion:" + self.obligation_id),
            *(Read("fact", item.kind, "#" + item.fact_id) for item in self.source_facts),
            *(
                Read("calculation", "*", "#" + item.calculation_id)
                for item in self.accepted_calculations
            ),
            *(
                (Read("fact", "external_completion", "#" + self.previous_completion_fact_id),)
                if self.previous_completion_fact_id
                else ()
            ),
        )

    def required_closed_periods(self):
        if self.obligation_kind != "quarterly_financial_report":
            return ()
        # Closing the last covered month also covers preceding empty months.
        return (self.end_period,)


def calculate_completion(version, context):
    fact = version.fact
    if not version.evidence:
        raise NeedsInformation("completion_evidence", "需要真实外部提交或完成凭据")
    if len(set(fact.adopted_evidence_digests)) != len(fact.adopted_evidence_digests) or not set(
        fact.adopted_evidence_digests
    ) <= set(version.evidence):
        raise KernelError("invalid_adopted_evidence", "原提交资料须引用本次保存的不可变证据")
    if not set(version.evidence) - set(fact.adopted_evidence_digests):
        raise NeedsInformation("completion_evidence", "办理回执须与原提交资料分别留存")
    obligation = context.one("external_obligation", "@" + fact.obligation_id)
    if any(
        getattr(fact, name) != getattr(obligation.fact, name)
        for name in ("obligation_kind", "start_period", "end_period")
    ):
        raise KernelError("obligation_changed", "外部义务的已确认范围发生变化")
    _accepted_history(fact, context.select)
    _adopted_source_history(fact, context.select)
    _validate_completion_chain(
        context.facts("external_completion", "completion:" + fact.obligation_id)
    )
    if fact.previous_completion_fact_id:
        previous = context.one("external_completion", "#" + fact.previous_completion_fact_id)
        if (
            previous.fact.obligation_id != fact.obligation_id
            or previous.id == version.id
            or fact.period < previous.fact.period
            or (
                fact.completion_date is not None
                and previous.fact.completion_date is not None
                and fact.completion_date < previous.fact.completion_date
            )
        ):
            raise KernelError(
                "invalid_completion_continuation", "补报必须接续同一义务的真实办理事实"
            )
    if obligation.fact.applicability != "required":
        raise KernelError("obligation_not_applicable", "明确不适用的事项不能登记实际办理")
    return Outcome(
        (),
        {
            "obligation_id": fact.obligation_id,
            "obligation_kind": fact.obligation_kind,
            "completion_status": fact.completion_status,
            "date_status": fact.date_status,
            "completion_date": fact.completion_date,
            "source_facts": [item.model_dump() for item in fact.source_facts],
            "adopted_evidence_digests": list(fact.adopted_evidence_digests),
            "accepted_calculations": [item.model_dump() for item in fact.accepted_calculations],
            "no_reportable_activity_confirmed": fact.no_reportable_activity_confirmed,
            "previous_completion_fact_id": fact.previous_completion_fact_id,
            "obligation_fact_id": fact.obligation_fact_id,
            "completion_evidence": list(version.evidence),
        },
    )


def _validate_completion_chain(versions):
    versions = tuple(versions)
    if not versions:
        return
    by_id = {item.id: item for item in versions}
    roots = [item for item in versions if item.fact.previous_completion_fact_id is None]
    if len(roots) != 1:
        raise KernelError("completion_continuation_required", "同一义务的再次办理须精确接续原办理")
    children = {}
    for item in versions:
        previous = item.fact.previous_completion_fact_id
        if previous is None:
            continue
        if previous not in by_id or previous in children:
            raise KernelError("invalid_completion_continuation", "补报须接续唯一的当前原办理版本")
        children[previous] = item.id
    if len(children) != len(versions) - 1:
        raise KernelError("invalid_completion_continuation", "补报接续链不完整")
    visited, cursor = set(), roots[0].id
    while cursor in children:
        if cursor in visited:
            raise KernelError("invalid_completion_continuation", "补报接续不能形成循环")
        visited.add(cursor)
        cursor = children[cursor]
    if len(visited) != len(versions) - 1:
        raise KernelError("invalid_completion_continuation", "补报接续链不完整")


def _adopted_source_history(fact, select):
    if len({(item.subject_id, item.fact_id) for item in fact.source_facts}) != len(
        fact.source_facts
    ):
        raise KernelError("duplicate_source", "原采用资料版本不能重复")
    allowed = {
        "individual_income_tax": {"payroll_tax_declaration_actual"},
        "contribution_declaration": {"payroll_contribution_actual"},
        "quarterly_tax": set(),
        "quarterly_financial_report": set(),
        "annual_income_tax": set(),
        "annual_business_report": set(),
    }[fact.obligation_kind]
    sources = []
    for ref in fact.source_facts:
        found = select(Read("fact", ref.kind, "#" + ref.fact_id))
        if len(found) != 1:
            raise NeedsInformation(
                "source_facts", "需要确实存在的原采用资料版本", sources=(ref.subject_id,)
            )
        source = found[0]
        source_period = getattr(source.fact, "tax_period", source.fact.period)
        if (
            source.subject_id != ref.subject_id
            or source.fact.kind != ref.kind
            or ref.kind not in allowed
            or not fact.start_period <= source_period <= fact.end_period
        ):
            raise KernelError("invalid_adopted_source", "原采用资料的身份、类型或期间与义务不符")
        if ref.kind == "payroll_contribution_actual" and not any(
            item.state == "declared" for item in source.fact.items
        ):
            raise KernelError("invalid_adopted_source", "未申报的社保明细不能作为已办理依据")
        sources.append(source)
    return tuple(sources)


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
                "*" not in OBLIGATION_DEFINITIONS[fact.obligation_kind].basis_kinds
                and item.kind not in OBLIGATION_DEFINITIONS[fact.obligation_kind].basis_kinds
            )
            or not item.result_digest
            or (
                OBLIGATION_DEFINITIONS[fact.obligation_kind].exclude_not_started
                and item.kind in PAYROLL_KINDS
                and item.values.get("tax_status") == "not_started"
            )
        ):
            raise KernelError("invalid_accepted_calculation", "接受版本的业务身份、类型或月份不符")
        accepted.append(item)
    return tuple(accepted)


def _result_basis(calculations, context):
    """Compare accounting meaning without rewriting the exact submitted basis."""
    result = set()
    for item in calculations:
        signature = context.accounting_signature(item)
        result.add(
            (item.subject_id, item.kind, item.period, signature["contract"], signature["digest"])
        )
    return result


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
    definition = OBLIGATION_DEFINITIONS[obligation.obligation_kind]
    issues = (
        _payroll_population_issues(
            profiles.values(), facts.values(), obligation.start_period, obligation.end_period
        )
        if definition.check_payroll_population
        else []
    )
    if "*" not in definition.basis_kinds:
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
            definition.exclude_not_started
            and calculations[subject].kind in PAYROLL_KINDS
            and calculations[subject].values.get("tax_status") == "not_started"
        )
    ), issues


class ExternalBasisReview(Fact):
    """Published comparison of one real completion with exact accounting versions."""

    kind: ClassVar[str] = "external_basis_review"
    lane: ClassVar[str] = "management"
    immutable: ClassVar[bool] = True
    identity_fields: ClassVar[tuple[str, ...]] = ("completion_id", "completion_fact_id")
    completion_id: str = Field(min_length=1)
    completion_fact_id: str = Field(min_length=1)
    obligation_id: str = Field(min_length=1)
    obligation_fact_id: str = Field(min_length=1)
    obligation_kind: ObligationKind
    start_period: YearMonth
    end_period: YearMonth
    source_facts: tuple[AdoptedSourceFact, ...] = ()
    adopted_calculations: tuple[AcceptedCalculation, ...] = ()
    reviewed_calculations: tuple[AcceptedCalculation, ...]
    review_result: Literal["matched", "difference_identified", "unestablished"]

    def scopes(self):
        return (str(self.period), "review:" + self.obligation_id)

    def reads(self):
        return (
            Read("fact", "external_completion", "#" + self.completion_fact_id),
            Read("fact", "external_obligation", "@" + self.obligation_id),
            Read("fact", "external_obligation", "#" + self.obligation_fact_id),
            *(Read("fact", item.kind, "#" + item.fact_id) for item in self.source_facts),
            *(
                Read("calculation", "*", "#" + item.calculation_id)
                for item in self.adopted_calculations
            ),
            *(
                Read("calculation", "*", "#" + item.calculation_id)
                for item in self.reviewed_calculations
            ),
            *_basis_reads(self.obligation_kind, self.start_period, self.end_period),
        )


def calculate_basis_review(version, context):
    fact = version.fact
    if not version.evidence:
        raise NeedsInformation("review_evidence", "需要实际申报与账务核对凭据")
    completion = context.one("external_completion", "#" + fact.completion_fact_id)
    obligation = context.one("external_obligation", "@" + fact.obligation_id)
    original = context.one("external_obligation", "#" + fact.obligation_fact_id)
    if (
        completion.subject_id != fact.completion_id
        or completion.fact.obligation_id != fact.obligation_id
        or completion.fact.obligation_fact_id != fact.obligation_fact_id
        or original.subject_id != fact.obligation_id
        or obligation.fact.applicability != "required"
        or fact.source_facts != completion.fact.source_facts
        or fact.adopted_calculations != completion.fact.accepted_calculations
        or any(
            getattr(fact, field) != getattr(obligation.fact, field)
            for field in ("obligation_kind", "start_period", "end_period")
        )
    ):
        raise KernelError("invalid_review_source", "核对未引用同一义务及真实办理的精确版本")
    current, issues = _basis_state(obligation.fact, context.select)
    selected = []
    if len({item.subject_id for item in fact.reviewed_calculations}) != len(
        fact.reviewed_calculations
    ):
        raise KernelError("duplicate_basis", "核对计算版本不能重复")
    for ref in fact.reviewed_calculations:
        found = context.select(Read("calculation", "*", "#" + ref.calculation_id))
        if len(found) != 1 or found[0].subject_id != ref.subject_id:
            raise NeedsInformation("reviewed_calculations", "需要确实存在的正式核算版本")
        selected.append(found[0])
    original_ids = {(item.subject_id, item.id) for item in selected}
    current_ids = {(item.subject_id, item.id) for item in current}
    if issues or original_ids != current_ids:
        return Outcome(
            (),
            {
                "completion_id": fact.completion_id,
                "completion_fact_id": fact.completion_fact_id,
                "obligation_id": fact.obligation_id,
                "obligation_fact_id": fact.obligation_fact_id,
                "review_result": "outdated",
                "reviewed_calculations": _reviewed_basis(selected),
                "review_evidence": list(version.evidence),
                "basis_issues": issues,
            },
        )
    comparisons = []
    original = []
    for ref in fact.adopted_calculations:
        found = context.select(Read("calculation", "*", "#" + ref.calculation_id))
        if len(found) != 1 or found[0].subject_id != ref.subject_id:
            raise NeedsInformation("adopted_calculations", "需要原采用的正式计算精确版本")
        original.append(found[0])
    if original:
        old_by_subject = {item.subject_id: item for item in original}
        if len(old_by_subject) != len(original) or set(old_by_subject) != {
            item.subject_id for item in current
        }:
            comparisons.append("different")
        else:
            for item in current:
                old = old_by_subject[item.subject_id]
                comparisons.append(
                    "matched"
                    if context.accounting_signature(old) == context.accounting_signature(item)
                    else "different"
                )
    source_result = _compare_reported_sources(fact, completion.fact, current, context)
    if source_result is not None:
        comparisons.append(source_result)
    proven_result = (
        "different"
        if "different" in comparisons
        else "matched"
        if comparisons and set(comparisons) == {"matched"}
        else "unestablished"
    )
    if fact.review_result == "matched" and proven_result != "matched":
        raise KernelError("review_not_proven", "原采用依据与当前核算未获证实一致，不能声明核对相符")
    if fact.review_result == "difference_identified" and proven_result != "different":
        raise KernelError("review_difference_not_proven", "原采用依据与当前核算未证实差异")
    if fact.review_result == "unestablished" and proven_result != "unestablished":
        raise KernelError("review_result_conflict", "核对结论须反映已证实的比较结果")
    return Outcome(
        (),
        {
            "completion_id": fact.completion_id,
            "completion_fact_id": fact.completion_fact_id,
            "obligation_id": fact.obligation_id,
            "obligation_fact_id": fact.obligation_fact_id,
            "review_result": fact.review_result,
            "source_facts": [item.model_dump() for item in fact.source_facts],
            "adopted_calculations": [item.model_dump() for item in fact.adopted_calculations],
            "reviewed_calculations": _reviewed_basis(current),
            "review_evidence": list(version.evidence),
        },
    )


def _compare_reported_sources(review, completion, current, context):
    """Only an explicitly comparable observed amount can establish agreement."""
    if not review.source_facts:
        if completion.no_reportable_activity_confirmed is True:
            return "different" if current else "matched"
        return None
    if review.obligation_kind not in {"individual_income_tax", "contribution_declaration"} or any(
        item.kind not in PAYROLL_KINDS for item in current
    ):
        return None
    reported = {}
    for ref in review.source_facts:
        found = context.select(Read("fact", ref.kind, "#" + ref.fact_id))
        if len(found) != 1 or found[0].subject_id != ref.subject_id:
            raise KernelError("invalid_review_source", "核对引用的原申报明细版本不符")
        source = found[0].fact
        if review.obligation_kind == "individual_income_tax":
            if source.kind != "payroll_tax_declaration_actual":
                return None
            key = (source.employee_id, source.tax_period)
            amount = source.declared_tax_fen
        else:
            if source.kind != "payroll_contribution_actual":
                return None
            key = (source.employee_id, source.period)
            amount = (
                sum_fen(item.employee_amount_fen for item in source.items),
                sum_fen(item.employer_amount_fen for item in source.items),
            )
        if key in reported:
            raise KernelError("duplicate_reported_source", "同一员工税期申报明细不能重复")
        reported[key] = amount
    calculated = {}
    for item in current:
        employee = item.values.get("employee_id")
        if review.obligation_kind == "individual_income_tax":
            amount = item.values.get("tax_fen")
        else:
            amount = (
                item.values.get("employee_contributions_fen"),
                item.values.get("employer_contributions_fen"),
            )
        if not employee or (
            type(amount) is not int
            if review.obligation_kind == "individual_income_tax"
            else any(type(value) is not int for value in amount)
        ):
            return None
        key = (employee, item.period)
        if review.obligation_kind == "individual_income_tax":
            calculated[key] = calculated.get(key, 0) + amount
        else:
            previous = calculated.get(key, (0, 0))
            calculated[key] = (previous[0] + amount[0], previous[1] + amount[1])
    if not current or set(reported) != set(calculated):
        return None
    return "matched" if reported == calculated else "different"


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


def _completion_confirmation_times(connection, fact_ids):
    """Read exact, uniquely attributable confirmation times without changing outcomes."""
    return {
        ident: timestamp
        for (_, ident), timestamp in recorded_times(
            connection, (("fact", ident) for ident in fact_ids)
        ).items()
    }


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
    registry.register(ExternalBasisReview, calculate_basis_review)
    registry.register_accounting(ExternalBasisReview.kind, compares_calculations=True)
    registry.register_readiness("payroll_presence", payroll_required_reads, payroll_required_work)


def required_reads(period):
    return (
        Read("fact", "external_obligation", "*"),
        Read("fact", "external_completion", "*"),
        Read("calculation", "external_completion", "*"),
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

        if not any(
            item.subject_id in completion_facts
            and item.fact_id == completion_facts[item.subject_id].id
            and completion_facts[item.subject_id].evidence
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
            if set(rules) != set(OBLIGATION_DEFINITIONS):
                raise NeedsInformation(
                    "filing_calendar_policy.rules", "需明确全部六类事项的周期规则"
                )
            deadlines = {
                (item.obligation_kind, item.end_period): item.due_date
                for item in policy.fact.deadlines
            }
            existing = self.store.select(connection, Read("fact", ExternalObligation.kind, "*"))
            candidates, reused, issues = [], [], []
            for kind in OBLIGATION_DEFINITIONS:
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
            if pending.intersection(item.subject_id for item in basis):
                issues.append({"field": "basis_pending", "message": "核算依据尚有待更正事项"})
            if fact.obligation_kind == "quarterly_financial_report":
                closed = connection.execute(
                    "SELECT 1 FROM period_close WHERE period>=? LIMIT 1",
                    (fact.end_period.ordinal,),
                ).fetchone()
                if closed is None:
                    raise KernelError("awaiting_close", "季度财报准备等待覆盖期末月份关账")
            return {
                "obligation_id": obligation_id,
                "obligation_fact_id": version.id,
                "obligation_kind": fact.obligation_kind,
                "start_period": fact.start_period,
                "end_period": fact.end_period,
                "candidate_calculations": [
                    {"subject_id": item.subject_id, "calculation_id": item.id}
                    for item in sorted(basis, key=lambda value: value.subject_id)
                ],
                "fact_issues": issues,
            }

    def query(self, period: str | None = None, *, as_of: str):
        """Read the shared company work list in one transaction snapshot."""
        from .worklist import Worklist

        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return Worklist(self.engine).query(connection, as_of=as_of, period=period)

    def _external_obligations(
        self,
        connection,
        period,
        as_of,
        *,
        reads=None,
        obligation_ids=None,
        summary=False,
    ):
        """Keep actual external events and accounting reviews independently visible."""
        from .query_reads import QueryReads

        reads = reads or QueryReads(self.engine, connection)
        day = ActualDate(as_of)
        if obligation_ids is None:
            facts = reads.select(Read("fact", "external_obligation", "*"))
        else:
            facts = tuple(
                reads.fact_versions(
                    row[0]
                    for row in connection.execute(
                        "SELECT a.fact_id FROM json_each(?) ids "
                        "JOIN fact_current a ON a.subject_id=ids.value "
                        "JOIN subject s ON s.id=a.subject_id WHERE s.kind='external_obligation'",
                        (json.dumps(sorted(obligation_ids)),),
                    )
                ).values()
            )
        facts = tuple(sorted(facts, key=lambda item: item.subject_id))
        requested = tuple(
            dict.fromkeys(
                read
                for version in facts
                for read in (
                    *version.fact.basis_reads(),
                    Read("fact", "external_completion", "completion:" + version.subject_id),
                    Read("calculation", "external_completion", "completion:" + version.subject_id),
                    Read("fact", "external_basis_review", "review:" + version.subject_id),
                    Read("calculation", "external_basis_review", "review:" + version.subject_id),
                )
            )
        )
        reads.prime_select(requested)
        candidate_ids = {item.subject_id for read in requested for item in reads.select(read)}
        pending = {
            row[0]
            for row in connection.execute(
                "SELECT p.subject_id FROM json_each(?) ids "
                "JOIN pending p ON p.subject_id=ids.value",
                (json.dumps(sorted(candidate_ids)),),
            )
        }
        all_completions = [
            item
            for version in facts
            for item in reads.select(
                Read("calculation", "external_completion", "completion:" + version.subject_id)
            )
        ]
        all_reviews = [
            item
            for version in facts
            for item in reads.select(
                Read("calculation", "external_basis_review", "review:" + version.subject_id)
            )
        ]
        publication_order = {
            row["calculation_id"]: row["sequence"]
            for row in connection.execute(
                "SELECT p.calculation_id,p.sequence FROM calculation_publication p "
                "JOIN json_each(?) ids ON ids.value=p.calculation_id",
                (json.dumps([item.id for item in (*all_completions, *all_reviews)]),),
            )
        }
        reads.prime_select(
            Read("fact", "external_completion", "#" + item.fact_id) for item in all_completions
        )
        confirmation_times = _completion_confirmation_times(
            connection, (item.fact_id for item in all_completions)
        )
        obligations, actual_counts, review_counts = [], {}, {}
        basis_issue_count = 0
        for version in facts:
            obligation = version.fact
            basis, issues = _basis_state(obligation, reads.select)
            if pending.intersection(item.subject_id for item in basis):
                issues.append({"field": "basis_pending", "message": "核算依据待更正"})
            basis_issue_count += len(issues)
            completions = reads.select(
                Read("calculation", "external_completion", "completion:" + version.subject_id)
            )
            completion_facts = {
                item.subject_id: item
                for item in reads.select(
                    Read("fact", "external_completion", "completion:" + version.subject_id)
                )
            }
            actual = []
            for calc in completions:
                source = completion_facts.get(calc.subject_id)
                current_fact = source is not None and calc.fact_id == source.id
                if not current_fact:
                    original = reads.select(Read("fact", "external_completion", "#" + calc.fact_id))
                    source = original[0] if len(original) == 1 else None
                if source is None or not source.evidence:
                    continue
                known = _completion_known_as_of(
                    calc.values["completion_date"], confirmation_times.get(calc.fact_id), day
                )
                actual.append((calc, source, known, current_fact))
            actual.sort(key=lambda entry: publication_order.get(entry[0].id, -1))
            known_actual = [entry for entry in actual if entry[2] and entry[3]]
            continued = {
                entry[1].fact.previous_completion_fact_id
                for entry in known_actual
                if entry[1].fact.previous_completion_fact_id is not None
            }
            terminals = [entry for entry in known_actual if entry[1].id not in continued]
            terminal = max(
                terminals,
                key=lambda entry: publication_order.get(entry[0].id, -1),
                default=None,
            )
            actual_status = (
                "not_applicable"
                if obligation.applicability == "not_applicable"
                else "completed"
                if known_actual
                else "due"
                if obligation.due_date is not None and obligation.due_date <= day
                else "pending"
            )
            reviews = reads.select(
                Read("calculation", "external_basis_review", "review:" + version.subject_id)
            )
            review_facts = {
                item.subject_id: item
                for item in reads.select(
                    Read("fact", "external_basis_review", "review:" + version.subject_id)
                )
            }
            current_ids = {(item.subject_id, item.id) for item in basis}
            review_status = (
                "not_applicable" if obligation.applicability == "not_applicable" else "not_reviewed"
            )
            latest_review = None
            for calc in sorted(
                reviews,
                key=lambda item: publication_order.get(item.id, -1),
                reverse=True,
            ):
                review_fact = review_facts.get(calc.subject_id)
                if review_fact is None or calc.fact_id != review_fact.id:
                    continue
                if (
                    terminal is None
                    or terminal[1].subject_id != review_fact.fact.completion_id
                    or terminal[1].id != review_fact.fact.completion_fact_id
                ):
                    continue
                latest_review = calc
                reviewed_ids = {
                    (item["subject_id"], item["calculation_id"])
                    for item in calc.values["reviewed_calculations"]
                }
                review_status = (
                    "outdated"
                    if issues or calc.subject_id in pending or reviewed_ids != current_ids
                    else "reviewed"
                    if calc.values["review_result"] == "matched"
                    else calc.values["review_result"]
                )
                break
            if known_actual and latest_review is None and review_status != "not_applicable":
                review_status = "not_reviewed"
            actual_counts[actual_status] = actual_counts.get(actual_status, 0) + 1
            review_counts[review_status] = review_counts.get(review_status, 0) + 1
            if summary:
                continue
            obligations.append(
                {
                    "id": version.subject_id,
                    "obligation_fact_id": version.id,
                    "kind": obligation.obligation_kind,
                    "start_period": obligation.start_period,
                    "end_period": obligation.end_period,
                    "due_date": obligation.due_date,
                    "status": actual_status,
                    "actual_completion_status": actual_status,
                    "basis_review_status": review_status,
                    "basis_review_calculation_id": latest_review.id if latest_review else None,
                    "basis_issues": issues,
                    "recorded_completions": [
                        {
                            "subject_id": calc.subject_id,
                            "calculation_id": calc.id,
                            "fact_id": source.id,
                            "current_fact": current_fact,
                            "completion_status": calc.values["completion_status"],
                            "completion_date": calc.values["completion_date"],
                            "source_facts": list(calc.values["source_facts"]),
                            "adopted_evidence_digests": list(
                                calc.values["adopted_evidence_digests"]
                            ),
                            "accepted_calculations": list(calc.values["accepted_calculations"]),
                            "previous_completion_fact_id": calc.values[
                                "previous_completion_fact_id"
                            ],
                            "known_as_of": known,
                            "confirmation_recorded_at": confirmation_times.get(calc.fact_id),
                            "completion_time_basis": (
                                "actual_date"
                                if calc.values["completion_date"] is not None
                                else "confirmation_recorded_at"
                                if calc.fact_id in confirmation_times
                                else "unestablished"
                            ),
                        }
                        for calc, source, known, current_fact in actual
                    ],
                }
            )
        if summary:
            return {
                "status": (
                    "completed"
                    if actual_counts and set(actual_counts) <= {"completed", "not_applicable"}
                    else "followup_required"
                    if actual_counts
                    else "unestablished"
                ),
                "obligation_count": len(facts),
                "actual_completion_status_counts": actual_counts,
                "basis_review_status_counts": review_counts,
                "basis_issue_count": basis_issue_count,
            }
        return obligations
