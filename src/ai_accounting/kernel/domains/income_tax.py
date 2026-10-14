"""Typed enterprise-income-tax prepayment inputs and detached calculation.

Accounting profit comes only from the kernel's shared report snapshot.  This
module neither reads storage nor accepts an AI-supplied replacement profit.
"""

from __future__ import annotations

import calendar
from decimal import Context as DecimalContext
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_accounting.policy_sources import OfficialPolicySourceURL

from ..contracts import Context, Fact, FactVersion, NeedsInformation, Read
from ..types import (
    MAX_FEN,
    ActualDate,
    EvidenceDigest,
    NonNegativeFen,
    PositiveFen,
    SubjectId,
    YearMonth,
    checked,
    is_sha256_hex,
)


def _field(meaning, *, default=..., precision, sources, **constraints):
    return Field(default=default, **constraints, json_schema_extra={"x-accounting-fact": {
        "role": "accounting", "meaning": meaning,
        "allowed_precision": list(precision), "reusable_sources": list(sources),
    }})


class IncomeTaxPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=100)
    source_url: OfficialPolicySourceURL
    effective_from: ActualDate
    effective_to: ActualDate | None
    standard_rate_percent: Decimal = Field(gt=0, le=100)
    small_profit_rate_percent: Decimal = Field(gt=0, le=100)
    small_profit_taxable_fraction: Decimal = Field(gt=0, le=1)
    small_profit_limit_fen: PositiveFen

    @field_validator(
        "standard_rate_percent", "small_profit_rate_percent", "small_profit_taxable_fraction",
        mode="before",
    )
    @classmethod
    def decimal_rate(cls, value):
        if not isinstance(value, (str, Decimal)):
            raise ValueError("income-tax rates require decimal strings, never binary floats")
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("income-tax rates require finite decimal values") from exc
        if not result.is_finite():
            raise ValueError("income-tax rates require finite decimal values")
        return result

    @model_validator(mode="after")
    def interval(self):
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("income-tax policy end precedes its effective start")
        return self


class IncomeTaxPolicyFact(Fact):
    kind: ClassVar[str] = "income_tax_policy"
    business_activity: ClassVar[bool] = False
    policy: IncomeTaxPolicy


class IncomeTaxPrepaymentBasis(Fact):
    """Evidenced company treatment and expressly known prior-loss deduction."""

    kind: ClassVar[str] = "income_tax_prepayment_basis"
    business_activity: ClassVar[bool] = False
    identity_fields: ClassVar[tuple[str, ...]] = ("year", "tax_period_end")
    year: int = _field("prepayment_tax_year", precision=("year",),
                       sources=("tax_registration",), ge=1, le=9999)
    tax_period_end: YearMonth = _field(
        "prepayment_cumulative_coverage_end", precision=("month",),
        sources=("tax_registration", "filed_tax_return"),
    )
    collection_method: Literal["actual_profit"] = _field(
        "confirmed_income_tax_prepayment_method", precision=("method",),
        sources=("tax_registration", "tax_authority_assessment"),
    )
    treatment: Literal["standard", "small_low_profit"] = _field(
        "confirmed_enterprise_income_tax_treatment", precision=("treatment",),
        sources=("tax_registration", "small_profit_qualification"),
    )
    basis_evidence: EvidenceDigest = _field(
        "prepayment_method_and_explicit_zero_loss_source", precision=("evidence_digest",),
        sources=("tax_registration", "owner_confirmation", "filed_tax_return"),
    )
    eligibility_evidence: EvidenceDigest | None = _field(
        "small_low_profit_eligibility_source", default=None, precision=("evidence_digest",),
        sources=("small_profit_qualification", "owner_confirmation"),
    )
    loss_deduction_fen: NonNegativeFen = _field(
        "confirmed_cumulative_prior_year_loss_deduction", precision=("integer_fen",),
        sources=("filed_tax_return", "tax_loss_schedule", "owner_confirmation"),
    )
    loss_evidence: EvidenceDigest | None = _field(
        "confirmed_prior_year_loss_deduction_source", default=None,
        precision=("evidence_digest",), sources=("filed_tax_return", "tax_loss_schedule"),
    )
    special_treatments: tuple[str, ...] = _field(
        "other_income_tax_adjustments_or_preferences_requiring_support",
        precision=("treatment_list",),
        sources=("filed_tax_return", "owner_confirmation"),
    )

    @model_validator(mode="after")
    def tax_coverage(self):
        if (int(self.tax_period_end[:4]) != self.year
                or int(self.tax_period_end[5:]) not in (3, 6, 9, 12)
                or self.tax_period_end > self.period):
            raise ValueError("prepayment basis requires a quarter end in its tax year")
        if any(not item.strip() for item in self.special_treatments):
            raise ValueError("special tax treatments cannot be blank")
        return self


class IncomeTaxAssessment(Fact):
    """Cumulative assessment with an optional kernel-computed provision."""

    kind: ClassVar[str] = "income_tax_assessment"
    identity_fields: ClassVar[tuple[str, ...]] = ("year",)
    cumulative_assessed_fen: NonNegativeFen | None = _field(
        "confirmed_cumulative_assessed_enterprise_income_tax", default=None,
        precision=("integer_fen",),
        sources=("owner_confirmation", "filed_tax_return", "tax_authority_assessment"),
    )
    assessment_basis: Literal[
        "confirmed_provision", "computed_provision", "filed_return", "annual_settlement"
    ]
    year: int = Field(ge=1, le=9999)
    policy_id: SubjectId | None = _field(
        "adopted_enterprise_income_tax_policy", default=None, precision=("subject_id",),
        sources=("income_tax_policy",),
    )
    basis_id: SubjectId | None = _field(
        "adopted_company_income_tax_prepayment_basis", default=None,
        precision=("subject_id",), sources=("income_tax_prepayment_basis",),
    )

    @model_validator(mode="after")
    def tax_year(self):
        if int(self.period[:4]) < self.year or (
            int(self.period[:4]) != self.year and self.assessment_basis != "annual_settlement"
        ):
            raise ValueError("income-tax assessment period must belong to the tax year")
        if self.assessment_basis == "computed_provision":
            if self.cumulative_assessed_fen is not None:
                raise ValueError("computed provisions cannot accept a supplied tax amount")
            if int(self.period[5:]) not in (3, 6, 9, 12):
                raise ValueError("computed income-tax provisions require a quarter end")
        elif self.policy_id is not None or self.basis_id is not None:
            raise ValueError("confirmed assessments do not accept computed policy references")
        return self

    def validate_registration(self):
        super().validate_registration()
        required = ("policy_id", "basis_id") if self.assessment_basis == "computed_provision" else (
            "cumulative_assessed_fen",
        )
        for field in required:
            if getattr(self, field) is None:
                raise NeedsInformation(
                    field, "缺少企业所得税核算依据",
                    precision=("subject_id",) if field.endswith("_id") else ("integer_fen",),
                )

    def scopes(self):
        return (str(self.period), f"income-tax:{self.year}",
                f"income-tax:{self.year}:{self.period}")

    def reads(self):
        reads = [
            Read("fact", self.kind, f"income-tax:{self.year}:{self.period}"),
            Read("calculation", self.kind, f"income-tax:{self.year}", self.period),
        ]
        for kind, subject_id in ((IncomeTaxPolicyFact.kind, self.policy_id),
                                 (IncomeTaxPrepaymentBasis.kind, self.basis_id)):
            if subject_id is not None:
                reads.append(Read("fact", kind, "@" + subject_id))
        return tuple(reads)

    def reads_for(self, subject_id):
        return self.reads() + (
            Read("fact", "income_tax_declaration_actual",
                 f"tax-declaration:{self.kind}:{subject_id}"),
        )


def _evidenced(source, field):
    evidence = getattr(source.fact, field)
    if evidence is None or evidence not in source.evidence:
        raise NeedsInformation(
            "income_tax_prepayment_basis." + field, "所得税依据必须引用已保全的对应原件",
            sources=(source.subject_id,), precision=("evidence_digest",),
        )


def _profit_snapshot(context, fact):
    snapshot = context.income_tax_snapshot
    if not snapshot:
        raise NeedsInformation("income_tax_snapshot", "须先建立共享财报税前利润及其精确来源")
    if snapshot.get("fact_issues"):
        raise NeedsInformation(list(snapshot["fact_issues"]))
    profit = snapshot.get("profit_before_tax_fen")
    if (snapshot.get("year") != fact.year
            or snapshot.get("quarter") != int(fact.period[5:]) // 3
            or snapshot.get("tax_period_end") != str(fact.period)
            or not is_sha256_hex(snapshot.get("basis_digest"))
            or not isinstance(snapshot.get("source_basis"), (list, tuple))
            or type(profit) is not int):
        raise NeedsInformation("income_tax_snapshot", "税前利润快照缺少同季度的金额或完整采用依据")
    sources = {"fact": set(), "calculation": set()}
    for read in context.income_tax_reads:
        selected = context.select(read)
        if not read.key.startswith("#") or len(selected) != 1 or selected[0].id != read.key[1:]:
            raise NeedsInformation("income_tax_snapshot.source_basis", "税前利润精确来源未完整建立")
        sources[read.source].add(selected[0].id)
    if (sources["calculation"] != set(snapshot.get("calculation_ids", ()))
            or sources["fact"] != set(snapshot.get("fact_ids", ()))):
        raise NeedsInformation("income_tax_snapshot.source_basis", "税前利润来源版本与快照不一致")
    return snapshot, checked(profit)


def calculate_computed_provision(version: FactVersion, context: Context) -> dict:
    """Return the cumulative estimate and its immutable accounting provenance."""
    fact = version.fact
    if fact.policy_id is None or fact.basis_id is None:
        raise NeedsInformation("policy_id,basis_id", "自动暂计须引用所得税政策和企业适用依据")
    policy_version = context.one(IncomeTaxPolicyFact.kind, "@" + fact.policy_id)
    basis_version = context.one(IncomeTaxPrepaymentBasis.kind, "@" + fact.basis_id)
    policy, basis = policy_version.fact.policy, basis_version.fact
    if basis.year != fact.year or basis.tax_period_end != fact.period:
        raise NeedsInformation("income_tax_prepayment_basis.tax_period_end",
                               "企业所得税依据须覆盖同一年度和季度", sources=(basis_version.id,),
                               precision=("month",))
    _evidenced(basis_version, "basis_evidence")
    if basis.treatment == "small_low_profit":
        _evidenced(basis_version, "eligibility_evidence")
    if basis.loss_deduction_fen:
        _evidenced(basis_version, "loss_evidence")
    if basis.special_treatments:
        raise NeedsInformation("income_tax_prepayment_basis.special_treatments",
                               "本次自动暂计尚不支持所列特殊所得税调整或优惠",
                               sources=(basis_version.id,))
    end = f"{fact.period}-{calendar.monthrange(fact.year, int(fact.period[5:]))[1]:02d}"
    if policy.effective_from > f"{fact.year:04d}-01-01" or (
        policy.effective_to is not None and policy.effective_to < end
    ):
        raise NeedsInformation("income_tax_policy.effective_from",
                               "所得税政策须完整覆盖本年累计核算期间", sources=(policy_version.id,),
                               precision=("day",))
    snapshot, profit = _profit_snapshot(context, fact)
    positive_profit = max(0, profit)
    if basis.loss_deduction_fen > positive_profit:
        raise NeedsInformation("income_tax_prepayment_basis.loss_deduction_fen",
                               "本年累计弥补亏损不得超过正的累计税前利润",
                               sources=(basis_version.id,), precision=("integer_fen",))
    taxable = positive_profit - basis.loss_deduction_fen
    if basis.treatment == "small_low_profit" and taxable > policy.small_profit_limit_fen:
        raise NeedsInformation("income_tax_prepayment_basis.treatment",
                               "累计计税利润超过所采用小微政策上限，须核对适用资格",
                               sources=(basis_version.id, policy_version.id))
    small = basis.treatment == "small_low_profit"
    rate = policy.small_profit_rate_percent if small else policy.standard_rate_percent
    fraction = policy.small_profit_taxable_fraction if small else Decimal(1)
    with localcontext(DecimalContext(prec=80)):
        amount = int((Decimal(taxable) * fraction * rate / Decimal(100)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP,
        ))
    if not 0 <= amount <= MAX_FEN:
        raise NeedsInformation("income_tax_snapshot.profit_before_tax_fen",
                               "所得税计算超出整数分金额范围", precision=("integer_fen",))
    return {
        "cumulative_assessed_fen": amount,
        "profit_before_tax_fen": profit,
        "loss_deduction_fen": basis.loss_deduction_fen,
        "taxable_profit_fen": taxable,
        "rate_percent": str(rate), "taxable_fraction": str(fraction),
        "policy_fact_id": policy_version.id, "policy_version": policy.version,
        "policy_source_url": policy.source_url, "basis_fact_id": basis_version.id,
        "treatment": basis.treatment, "basis_digest": snapshot["basis_digest"],
        "source_basis": list(snapshot["source_basis"]),
    }


def register(registry):
    registry.register(IncomeTaxPolicyFact)
    registry.register(IncomeTaxPrepaymentBasis)
