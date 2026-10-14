"""Source-confirmed remuneration deductions; no tax assessment or cash inference."""

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Context, Fact, FactVersion, KernelError, NeedsInformation, Read
from .types import (
    ActualDate,
    EvidenceDigest,
    NonNegativeFen,
    SubjectId,
    YearMonth,
    canonical,
    digest,
    sum_fen,
)

REMUNERATION_TAX_REQUIRED_KINDS = ("payroll", "payroll_bounded", "annual_bonus", "labor")
REMUNERATION_KINDS = (*REMUNERATION_TAX_REQUIRED_KINDS, "labor_accrual", "labor_project_cost")


def tax_basis_scope(kind: str, subject_id: str) -> str:
    return f"remuneration-tax:{kind}:{subject_id}"


def remuneration_tax_period_scope(period: YearMonth) -> str:
    return f"remuneration-tax-period:{period}"


def pooled_tax_scope(proof) -> str:
    return f"remuneration-tax-pool:{proof.evidence_digest}:{digest(proof.source_location).hex()}"


def _pool_allocation_data(proof):
    data = proof.model_dump(mode="json")
    data["allocations"] = sorted(
        data["allocations"], key=lambda row: (row["source_kind"], row["source_id"], row["period"])
    )
    return canonical(data)


class HistoricalWithholdingOrigin(BaseModel):
    """An explicitly adopted exact individual withholding record, never a pooled payment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["payroll_withholding_actual"]
    subject_id: SubjectId
    fact_id: SubjectId


class PooledTaxAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    source_kind: Literal[
        "payroll", "payroll_bounded", "annual_bonus", "labor", "labor_accrual", "labor_project_cost"
    ]
    source_id: SubjectId
    period: YearMonth
    tax_amount_fen: NonNegativeFen


class PooledTaxProof(BaseModel):
    """Complete owner-confirmed split of one identified source location."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence_digest: EvidenceDigest
    source_location: str = Field(min_length=1, description="原件中对应合并税额的页、表或行位置")
    total_tax_fen: NonNegativeFen
    allocations: tuple[PooledTaxAllocation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_split(self):
        keys = [(item.source_kind, item.source_id, item.period) for item in self.allocations]
        if len(keys) != len(set(keys)):
            raise ValueError("pooled tax allocations must identify each source exactly once")
        if sum_fen(item.tax_amount_fen for item in self.allocations) != self.total_tax_fen:
            raise ValueError("the complete individual split must equal the pooled tax amount")
        return self


class RemunerationTaxBasis(Fact):
    kind: ClassVar[str] = "remuneration_tax_basis"
    identity_fields: ClassVar[tuple[str, ...]] = ("source_kind", "source_id", "period")
    period: YearMonth = Field(
        description="所引用报酬来源的核算所属期，不能用申报或缴款月替代",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "referenced_remuneration_accounting_period",
                "allowed_precision": ["month"],
                "reusable_sources": ["registered_remuneration"],
            }
        },
    )
    source_kind: Literal[
        "payroll", "payroll_bounded", "annual_bonus", "labor", "labor_accrual", "labor_project_cost"
    ]
    source_id: SubjectId = Field(
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "confirmed_deduction_target_subject",
                "reusable_sources": ["registered_remuneration"],
            }
        },
    )
    source_fact_id: SubjectId = Field(
        description="税额确认时采用的精确报酬事实版本；人员或毛额变化须重新确认税依据",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "exact_confirmed_remuneration_source_fact",
                "reusable_sources": ["registered_remuneration_revision"],
            }
        },
    )
    tax_period: YearMonth = Field(
        description="原始税额依据明确的税期，独立于报酬核算所属期及实际缴款月",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "confirmed_deduction_tax_period",
                "allowed_precision": ["month"],
                "reusable_sources": ["final_remuneration_statement", "actual_withholding_record"],
            }
        },
    )
    tax_amount_fen: NonNegativeFen = Field(
        description="原始最终报酬表或实际扣税记录明确的采用税额；零额也须明确确认",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "confirmed_remuneration_deduction",
                "allowed_precision": ["integer_fen"],
                "reusable_sources": ["final_remuneration_statement", "actual_withholding_record"],
            }
        },
    )
    basis_type: Literal["final_statement", "actual_withholding"]
    confirmed: Literal[True]
    withholding_date: ActualDate | None = Field(
        default=None,
        description="仅实际扣税原件明确的扣税日；未知不补日",
        json_schema_extra={
            "x-accounting-fact": {
                "role": "management",
                "meaning": "observed_withholding_date",
                "allowed_precision": ["day"],
                "reusable_sources": ["actual_withholding_record"],
            }
        },
    )
    origin_references: tuple[HistoricalWithholdingOrigin, ...] = ()
    pooled_tax_proof: PooledTaxProof | None = None

    @model_validator(mode="after")
    def exact_origin(self):
        if self.withholding_date is not None and self.basis_type != "actual_withholding":
            raise ValueError("only actual withholding may establish a withholding date")
        if len(self.origin_references) > 1:
            raise ValueError("one individual deduction cannot adopt multiple pooled tax records")
        if self.origin_references and (
            self.source_kind not in ("payroll", "payroll_bounded")
            or self.basis_type != "actual_withholding"
        ):
            raise ValueError("historical wage withholding may only support actual wage withholding")
        if self.pooled_tax_proof is not None:
            matching = [
                item
                for item in self.pooled_tax_proof.allocations
                if item.source_kind == self.source_kind
                and item.source_id == self.source_id
                and item.period == self.period
            ]
            if len(matching) != 1 or matching[0].tax_amount_fen != self.tax_amount_fen:
                raise ValueError(
                    "the current source deduction must match its individual pooled allocation"
                )
        return self

    def scopes(self):
        return (
            tax_basis_scope(self.source_kind, self.source_id),
            remuneration_tax_period_scope(self.tax_period),
            *(
                (pooled_tax_scope(self.pooled_tax_proof),)
                if self.pooled_tax_proof is not None
                else ()
            ),
        )

    def reads(self):
        return (
            Read("fact", self.source_kind, "@" + self.source_id),
            Read("fact", self.source_kind, "#" + self.source_fact_id),
            *(Read("fact", ref.kind, "#" + ref.fact_id) for ref in self.origin_references),
        )


def remuneration_tax_reads(kind, subject_id):
    return (Read("fact", RemunerationTaxBasis.kind, tax_basis_scope(kind, subject_id)),)


def remuneration_tax_reads_after(kind, subject_id, context):
    sources = context.facts(RemunerationTaxBasis.kind, tax_basis_scope(kind, subject_id))
    origins = [
        Read("fact", ref.kind, "#" + ref.fact_id)
        for source in sources
        for ref in source.fact.origin_references
    ]
    origins.extend(
        Read("fact", source.fact.source_kind, "#" + source.fact.source_fact_id)
        for source in sources
    )
    targets = [
        Read("fact", item.source_kind, str(item.period))
        for source in sources
        if source.fact.pooled_tax_proof is not None
        for item in source.fact.pooled_tax_proof.allocations
    ]
    pools = [
        Read("fact", RemunerationTaxBasis.kind, pooled_tax_scope(source.fact.pooled_tax_proof))
        for source in sources
        if source.fact.pooled_tax_proof is not None
    ]
    return tuple(dict.fromkeys((*origins, *targets, *pools)))


def resolve_remuneration_tax(version: FactVersion, context: Context, *, required=True):
    sources = context.facts(
        RemunerationTaxBasis.kind, tax_basis_scope(version.fact.kind, version.subject_id)
    )
    if len(sources) > 1:
        raise KernelError("ambiguous_remuneration_tax_basis", "同一报酬存在多个当前税额依据")
    if not sources:
        if not required:
            return None
        raise NeedsInformation(
            "remuneration_tax_basis",
            "需要本项报酬有依据的最终扣税额；明确零额也须确认",
            sources=(
                version.subject_id,
                "final_remuneration_statement",
                "actual_withholding_record",
            ),
            precision=("integer_fen",),
        )
    source = sources[0]
    fact = source.fact
    if (
        fact.source_kind != version.fact.kind
        or fact.source_id != version.subject_id
        or (fact.period != version.fact.period)
    ):
        raise KernelError(
            "remuneration_tax_source_conflict", "税额依据与报酬来源或核算所属期不一致"
        )
    if not source.evidence:
        raise NeedsInformation(
            "remuneration_tax_basis.evidence", "采用扣税金额需要原始依据或负责人确认"
        )
    adopted = context.one(fact.source_kind, "#" + fact.source_fact_id)
    if (
        adopted.subject_id != version.subject_id
        or adopted.fact.kind != version.fact.kind
        or (adopted.fact.period != version.fact.period)
    ):
        raise KernelError(
            "remuneration_tax_source_conflict", "税额精确依据不属于当前报酬人员与所属期"
        )
    fields = {
        "payroll": ("employee_id", "accounting_gross_salary_fen"),
        "payroll_bounded": ("employee_id", "accounting_gross_salary_fen"),
        "annual_bonus": ("employee_id", "bonus_fen", "income_date"),
        "labor": (
            "person_id",
            "remuneration_method",
            "fixed_fee_fen",
            "commission_base_fen",
            "commission_rate_ppm",
            "income_date",
        ),
        "labor_accrual": ("person_id", "gross_fee_fen"),
        "labor_project_cost": ("person_id", "project_id", "gross_fee_fen"),
    }[fact.source_kind]
    if any(getattr(adopted.fact, field) != getattr(version.fact, field) for field in fields):
        raise NeedsInformation(
            "remuneration_tax_basis.source_fact_id",
            "税额采用的人员或报酬事实已变化，需要明确当前来源的税额依据",
            sources=(version.subject_id, version.id),
            precision=("exact_fact_revision",),
        )
    pool = fact.pooled_tax_proof
    if pool is not None:
        if pool.evidence_digest not in source.evidence:
            raise NeedsInformation(
                "pooled_tax_proof.evidence_digest", "合并税额分配须保留明确引用的原件"
            )
        for peer in context.facts(RemunerationTaxBasis.kind, pooled_tax_scope(pool)):
            other = peer.fact.pooled_tax_proof
            if (
                other is None
                or peer.fact.tax_period != fact.tax_period
                or _pool_allocation_data(other) != _pool_allocation_data(pool)
            ):
                raise KernelError(
                    "conflicting_pooled_tax_allocation", "同一合并税额原位置存在冲突的完整分配依据"
                )
        for allocation in pool.allocations:
            targets = [
                target
                for target in context.facts(allocation.source_kind, str(allocation.period))
                if target.subject_id == allocation.source_id
            ]
            if len(targets) != 1:
                raise NeedsInformation(
                    "pooled_tax_proof.allocations",
                    "合并税额完整分配须引用已明确的报酬来源",
                    sources=(allocation.source_id,),
                )
            target = targets[0]
            if target.fact.period != allocation.period:
                raise KernelError(
                    "pooled_tax_target_conflict", "合并税额分配的报酬来源所属期不一致"
                )
    for ref in fact.origin_references:
        origin = context.one(ref.kind, "#" + ref.fact_id)
        if (
            origin.subject_id != ref.subject_id
            or not origin.evidence
            or (
                origin.fact.employee_id != getattr(version.fact, "employee_id", None)
                or origin.fact.period != fact.period
                or origin.fact.withheld_tax_fen != fact.tax_amount_fen
                or origin.fact.withholding_confirmed is not True
            )
        ):
            raise KernelError(
                "remuneration_tax_origin_conflict", "明确引用的原实际扣税人员、税期或金额不一致"
            )
    return source


def tax_basis_values(version):
    return {
        "fact_id": version.id,
        "subject_id": version.subject_id,
        "revision": version.revision,
        **version.fact.model_dump(mode="json"),
        "evidence": list(version.evidence),
        "cash_payment_recorded": False,
    }


def register(registry):
    registry.register(RemunerationTaxBasis)
