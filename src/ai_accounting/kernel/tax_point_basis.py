"""Evidence-bound VAT points, independent of payment calculations.

2026 VAT Law article 28 and Implementation Regulations article 39.  Dates
below describe confirmed source events. Explicitly sourced tax dates and optional
detailed evidence share this module. One interval of a business's gross amount
is taxed at its earliest established trigger, even when an invoice, contractual
right and receipt overlap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .contracts import Fact, KernelError, NeedsInformation, Read, Registry
from .types import (
    ActualDate,
    EvidenceDigest,
    NonNegativeFen,
    PositiveFen,
    SubjectId,
    YearMonth,
    sum_fen,
)

RULE_VERSION = "cn-vat-tax-point-2026-v1"
RULE_SOURCES = (
    "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5237365/content.html",
    "https://fgk.chinatax.gov.cn/zcfgk/c100010/c5246349/content.html",
    "https://tianjin.chinatax.gov.cn/11200000000/0300/030004/03000418/20260203100537459.shtml",
)
BusinessKind = Literal["service_sale", "advance", "advance_fulfillment", "asset_disposal"]


def _field(meaning, *, sources, precision=None, default=...):
    metadata = {
        "role": "accounting",
        "meaning": meaning,
        "reusable_sources": list(sources),
        "constraint": "须有精确原件来源，不推定或补造",
    }
    if precision:
        metadata["allowed_precision"] = list(precision)
    return Field(default=default, json_schema_extra={"x-accounting-fact": metadata})


class TaxPointSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal[
        "material_source_v2", "bank_statement", "payment", "cash_payment", "platform_payment"
    ]
    subject_id: SubjectId
    fact_id: str = Field(min_length=1, max_length=200)
    evidence_digest: EvidenceDigest
    location: str = Field(min_length=1, max_length=500)


class TaxPointCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    trigger: Literal["invoice", "receipt", "contract"]
    period: YearMonth
    actual_date: ActualDate | None = None
    gross_start_fen: NonNegativeFen
    gross_end_fen: PositiveFen
    source: TaxPointSource = _field(
        "tax_trigger_source_and_business_receipt_allocation",
        sources=(
            "payment.allocations",
            "cash_payment.allocations",
            "platform_payment.allocations",
            "contract",
            "invoice",
        ),
    )
    receipt_stage: Literal["before_transaction", "during_or_after_transaction"] | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.gross_end_fen <= self.gross_start_fen:
            raise ValueError("tax trigger must cover a nonempty gross interval")
        if self.actual_date is not None and self.actual_date.period != self.period:
            raise ValueError("source event day must belong to its source month")
        if self.trigger == "receipt" and (self.actual_date is None or self.receipt_stage is None):
            raise ValueError("actual receipts require their real day and transaction stage")
        if self.trigger != "receipt" and self.receipt_stage is not None:
            raise ValueError("only receipts have a transaction stage")
        return self


class TaxPointBasis(Fact):
    kind: ClassVar[str] = "tax_point_basis"
    identity_fields: ClassVar[tuple[str, ...]] = ("business_kind", "business_id")
    business_activity: ClassVar[bool] = False
    business_kind: BusinessKind
    business_id: SubjectId
    service_pattern: Literal["single", "phased"] | None = _field(
        "service_delivery_pattern", sources=("service_contract",), default=None
    )
    first_service_started: StrictBool | None = _field(
        "actual_first_service_started", sources=("first_service_record",), default=None
    )
    first_service_period: YearMonth | None = _field(
        "actual_first_service_month",
        sources=("first_service_record",),
        precision=("month", "day"),
        default=None,
    )
    first_service_date: ActualDate | None = _field(
        "actual_first_service_day",
        sources=("first_service_record",),
        precision=("day",),
        default=None,
    )
    first_service_source: TaxPointSource | None = _field(
        "actual_first_service_source", sources=("first_service_record",), default=None
    )
    written_contract_status: Literal[
        "payment_date_set", "no_written_contract", "no_payment_date"
    ] = _field(
        "written_contract_payment_right_status", sources=("contract", "confirmed_contract_review")
    )
    contract_payment_period: YearMonth | None = _field(
        "contractual_payment_right_month",
        sources=("contract",),
        precision=("month", "day"),
        default=None,
    )
    contract_payment_date: ActualDate | None = _field(
        "contractual_payment_right_day", sources=("contract",), precision=("day",), default=None
    )
    contract_source: TaxPointSource = _field(
        "contract_review_source", sources=("contract", "confirmed_contract_review")
    )
    performance_completion_period: YearMonth | None = _field(
        "actual_service_completion_month",
        sources=("service_completion",),
        precision=("month", "day"),
        default=None,
    )
    performance_completion_date: ActualDate | None = _field(
        "actual_service_completion_day",
        sources=("service_completion",),
        precision=("day",),
        default=None,
    )
    performance_source: TaxPointSource | None = _field(
        "service_completion_source", sources=("service_completion",), default=None
    )
    candidates: tuple[TaxPointCandidate, ...] = _field(
        "earliest_tax_trigger_candidates_with_exact_receipt_allocations",
        sources=(
            "payment.allocations",
            "cash_payment.allocations",
            "platform_payment.allocations",
            "invoice",
            "contract",
        ),
        default=(),
    )
    reviewed_through_period: YearMonth = _field(
        "tax_trigger_review_coverage",
        sources=("invoice_register", "contract", "receipt_register"),
        precision=("month",),
    )
    earlier_candidates_reviewed: StrictBool = _field(
        "earlier_invoice_entitlement_receipt_reviewed",
        sources=("invoice_register", "contract", "receipt_register"),
    )
    review_source: TaxPointSource = _field(
        "complete_tax_trigger_review_source",
        sources=("invoice_register", "contract", "receipt_register"),
    )
    rule_version: Literal["cn-vat-tax-point-2026-v1"] = RULE_VERSION

    @model_validator(mode="after")
    def source_dates(self):
        for month, day in (
            (self.contract_payment_period, self.contract_payment_date),
            (self.performance_completion_period, self.performance_completion_date),
            (self.first_service_period, self.first_service_date),
        ):
            if day is not None and (month is None or day.period != month):
                raise ValueError("source day requires its matching explicit source month")
        if self.written_contract_status == "payment_date_set":
            if self.contract_payment_period is None:
                raise ValueError("a dated payment right needs its contractual source month")
        elif self.contract_payment_period is not None or self.contract_payment_date is not None:
            raise ValueError("absence of a contractual payment date cannot contain a chosen date")
        if self.performance_completion_period is not None and self.performance_source is None:
            raise ValueError("service completion needs its own source")
        if (
            self.performance_completion_period is not None
            and self.performance_completion_period > self.period
        ):
            raise ValueError("actual service completion cannot be projected into the future")
        if self.first_service_started is True and (
            self.first_service_period is None or self.first_service_source is None
        ):
            raise ValueError("an actual first service requires its own period and source")
        if self.first_service_started is not True and (
            self.first_service_period is not None or self.first_service_date is not None
        ):
            raise ValueError("a service that has not started cannot contain an invented start date")
        if self.first_service_period is not None and self.first_service_period > self.period:
            raise ValueError("actual first service cannot be projected into the future")
        if self.reviewed_through_period > self.period:
            raise ValueError("review cannot attest a future month")
        if any(c.trigger != "contract" and c.period > self.period for c in self.candidates):
            raise ValueError("an actual invoice or receipt cannot be projected into the future")
        identities = [
            (c.trigger, c.source.fact_id, c.source.location, c.gross_start_fen, c.gross_end_fen)
            for c in self.candidates
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate source tax trigger")
        return self

    def scopes(self):
        return (str(self.period), tax_basis_scope(self.business_kind, self.business_id))


def tax_basis_scope(kind, business_id):
    return f"tax-basis:{kind}:{business_id}"


def tax_basis_reads(kind, business_id):
    return (Read("fact", "tax_point_basis", tax_basis_scope(kind, business_id)),)


def _sources(fact):
    return (
        fact.contract_source,
        fact.review_source,
        *((fact.performance_source,) if fact.performance_source is not None else ()),
        *((fact.first_service_source,) if fact.first_service_source is not None else ()),
        *(candidate.source for candidate in fact.candidates),
    )


def tax_basis_source_reads(ctx, kind, business_id):
    return tuple(
        sorted(
            {
                Read("fact", source.kind, "#" + source.fact_id)
                for version in ctx.select(tax_basis_reads(kind, business_id)[0])
                for source in _sources(version.fact)
            }
        )
    )


def _issue(field, message, *, sources=(), precision=()):
    raise NeedsInformation(field, message, sources=sources, precision=precision)


def _verify_source(ctx, reference, basis_version):
    found = ctx.select(Read("fact", reference.kind, "#" + reference.fact_id))
    if len(found) != 1 or found[0].subject_id != reference.subject_id:
        _issue(
            "tax_point_basis.source",
            "需要确实存在且身份一致的精确税点原件事实版本",
            sources=(reference.subject_id,),
        )
    source = found[0]
    if (
        reference.evidence_digest not in source.evidence
        or reference.evidence_digest not in basis_version.evidence
    ):
        _issue(
            "tax_point_basis.source.evidence_digest",
            "税点依据与原件事实须共同引用该不可变原件",
            sources=(reference.subject_id,),
        )
    if reference.kind == "material_source_v2":
        if source.fact.evidence_digest != reference.evidence_digest:
            _issue(
                "tax_point_basis.source.evidence_digest",
                "税点引用不属于所选原件",
                sources=(reference.subject_id,),
            )
        passages = source.fact.specification.passages
        if passages and not any(p.location == reference.location for p in passages):
            _issue(
                "tax_point_basis.source.location",
                "税点原件位置未在所选精确来源中保全",
                sources=(reference.subject_id,),
            )
    return source


@dataclass(frozen=True)
class TaxPointInterval:
    gross_start_fen: int
    gross_end_fen: int
    period: YearMonth
    actual_date: ActualDate | None
    trigger: str
    source: TaxPointSource
    actual_date_field: str


@dataclass(frozen=True)
class TaxPointResolution:
    basis_id: str
    intervals: tuple[TaxPointInterval, ...]
    service_pattern: str | None

    def public(self):
        return {
            "basis_fact_id": self.basis_id,
            "rule_version": RULE_VERSION,
            "rule_effective_from": "2026-01-01",
            "service_pattern": self.service_pattern,
            "official_sources": list(RULE_SOURCES),
            "intervals": [
                {
                    "gross_start_fen": i.gross_start_fen,
                    "gross_end_fen": i.gross_end_fen,
                    "period": str(i.period),
                    "actual_date": i.actual_date,
                    "actual_date_field": i.actual_date_field,
                    "trigger": i.trigger,
                    "source": i.source.model_dump(mode="json"),
                }
                for i in self.intervals
            ],
        }


def resolve_tax_point(ctx, kind, business_id, gross_fen, as_of):
    bases = ctx.select(tax_basis_reads(kind, business_id)[0])
    if len(bases) != 1:
        _issue(
            "tax_point_basis",
            "需要本项业务唯一的有源税点依据，不由收入月份或晚到回款推定",
            sources=(tax_basis_scope(kind, business_id),),
        )
    version = bases[0]
    fact = version.fact
    if gross_fen <= 0:
        _issue(
            "tax_point_basis",
            "现有税点范围须有明确的正金额；零对价出售还需要有源增值税计税销售额或无需计税依据，不能由成交价零推定",
            sources=(version.subject_id,),
        )
    if not fact.earlier_candidates_reviewed or fact.reviewed_through_period < as_of:
        _issue(
            "tax_point_basis.earlier_candidates_reviewed",
            "须完整核对本期及此前的开票、合同收款权和实际收款候选",
            sources=(version.subject_id,),
        )
    verified_sources = {
        reference: _verify_source(ctx, reference, version)
        for reference in dict.fromkeys(_sources(fact))
    }
    if kind != "asset_disposal" and fact.service_pattern is None:
        _issue(
            "tax_point_basis.service_pattern",
            "需要有源单次或分期分次服务事实，不推定预收计税规则",
            sources=(version.subject_id,),
        )
    if fact.service_pattern == "phased" and fact.first_service_started is None:
        _issue(
            "tax_point_basis.first_service_started",
            "分期分次服务须明确首次服务是否已实际开始",
            sources=(version.subject_id,),
        )
    candidates = list(fact.candidates)
    for candidate in fact.candidates:
        if candidate.trigger == "contract" and (
            fact.written_contract_status != "payment_date_set"
            or candidate.period != fact.contract_payment_period
            or candidate.actual_date != fact.contract_payment_date
            or candidate.source != fact.contract_source
        ):
            _issue(
                "tax_point_basis.candidates",
                "候选不能改写已明确合同付款权的日期或来源；分段合同须有独立的可核对事实",
                sources=(version.subject_id,),
            )
    if fact.written_contract_status == "payment_date_set":
        candidates.append(
            TaxPointCandidate.model_validate_json(
                json.dumps(
                    {
                        "trigger": "contract",
                        "period": str(fact.contract_payment_period),
                        "actual_date": fact.contract_payment_date,
                        "gross_start_fen": 0,
                        "gross_end_fen": gross_fen,
                        "source": fact.contract_source.model_dump(mode="json"),
                    }
                )
            )
        )
    elif fact.performance_completion_period is not None:
        candidates.append(
            TaxPointCandidate.model_validate_json(
                json.dumps(
                    {
                        "trigger": "contract",
                        "period": str(fact.performance_completion_period),
                        "actual_date": fact.performance_completion_date,
                        "gross_start_fen": 0,
                        "gross_end_fen": gross_fen,
                        "source": fact.performance_source.model_dump(mode="json"),
                    }
                )
            )
        )
    elif kind != "advance":
        _issue(
            "tax_point_basis.performance_completion_period",
            "没有书面合同付款日时，需要有源服务完成期",
            sources=(version.subject_id,),
            precision=("month", "day"),
        )
    if fact.service_pattern == "phased":
        prepaid = [
            c
            for c in fact.candidates
            if c.trigger == "receipt" and c.receipt_stage == "before_transaction"
        ]
        for receipt in prepaid:
            triggers = []
            if fact.first_service_started is True:
                triggers.append(
                    (fact.first_service_period, fact.first_service_date, fact.first_service_source)
                )
            if fact.written_contract_status == "payment_date_set":
                triggers.append(
                    (fact.contract_payment_period, fact.contract_payment_date, fact.contract_source)
                )
            if triggers:
                month, day, reference = min(triggers, key=lambda t: (t[0], t[1] or ""))
                candidates.append(
                    TaxPointCandidate.model_validate_json(
                        json.dumps(
                            {
                                "trigger": "contract",
                                "period": str(month),
                                "actual_date": day,
                                "gross_start_fen": receipt.gross_start_fen,
                                "gross_end_fen": receipt.gross_end_fen,
                                "source": reference.model_dump(mode="json"),
                            }
                        )
                    )
                )
    receipts = sorted(
        (c for c in fact.candidates if c.trigger == "receipt"),
        key=lambda c: (c.actual_date, c.source.fact_id, c.source.location),
    )
    received = 0
    consumed_receipts = set()
    for receipt in receipts:
        source = receipt.source
        identity = (
            (source.kind, source.subject_id, source.location)
            if source.kind in {"bank_statement", "material_source_v2"}
            else (source.kind, source.subject_id)
        )
        if identity in consumed_receipts:
            _issue(
                "tax_point_basis.candidates.source",
                "同一真实收款原行或付款业务只能消费一次；不能通过不同事实版本、位置或金额分段重复计税",
                sources=(source.subject_id,),
            )
        consumed_receipts.add(identity)
        if receipt.gross_start_fen != received:
            _issue(
                "tax_point_basis.candidates.gross_start_fen",
                "真实收款范围须按实际先后累计衔接，不能重叠或跳过已收金额",
                sources=(version.subject_id,),
            )
        received = receipt.gross_end_fen
    for candidate in candidates:
        if candidate.gross_end_fen > gross_fen:
            _issue(
                "tax_point_basis.candidates.gross_end_fen",
                "税点来源范围不能超过本项业务含税额",
                sources=(version.subject_id,),
            )
        if candidate.period < "2026-01" and not (
            candidate.trigger == "receipt" and candidate.receipt_stage == "before_transaction"
        ):
            _issue(
                "tax_point_basis.rule_version",
                "本规则只覆盖2026年起明确的税点事实",
                sources=(version.subject_id,),
            )
        source = verified_sources[candidate.source]
        if candidate.trigger == "receipt" and candidate.source.kind in {
            "bank_statement",
            "material_source_v2",
        }:
            _issue(
                "tax_point_basis.candidates.source",
                "原件总收款不能单独证明本业务收款归属；须复用已有类型化收款事实中本业务的明确分配，不等待其凭证计算",
                sources=(
                    source.subject_id,
                    "payment.allocations",
                    "cash_payment.allocations",
                    "platform_payment.allocations",
                ),
            )
        if candidate.trigger == "receipt" and candidate.source.kind in {
            "payment",
            "cash_payment",
            "platform_payment",
        }:
            payment = source.fact
            allocated = sum(
                a.amount_fen
                for a in payment.allocations
                if a.source_kind == kind and a.source_id == business_id
            )
            if (
                payment.direction != "inflow"
                or payment.actual_date != candidate.actual_date
                or allocated != candidate.gross_end_fen - candidate.gross_start_fen
            ):
                _issue(
                    "tax_point_basis.candidates.source",
                    "税点收款日期与金额范围须匹配本业务真实收款事实",
                    sources=(source.subject_id,),
                )
    candidates = [
        c
        for c in candidates
        if c.trigger != "receipt" or c.receipt_stage == "during_or_after_transaction"
    ]
    boundaries = sorted(
        {
            0,
            gross_fen,
            *(c.gross_start_fen for c in candidates),
            *(c.gross_end_fen for c in candidates),
        }
    )
    intervals = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        covering = [c for c in candidates if c.gross_start_fen <= start and c.gross_end_fen >= end]
        if not covering:
            continue
        earliest_period = min(c.period for c in covering)
        earliest = [c for c in covering if c.period == earliest_period]
        # A source month establishes a tax month, never an invented actual day.
        selected = min(
            earliest, key=lambda c: (c.actual_date or "", c.trigger != "invoice", c.source.fact_id)
        )
        actual_date_field = "tax_point_basis.candidates.actual_date"
        if selected.trigger == "contract":
            for month_name, day_name, source_name in (
                ("contract_payment_period", "contract_payment_date", "contract_source"),
                (
                    "performance_completion_period",
                    "performance_completion_date",
                    "performance_source",
                ),
                ("first_service_period", "first_service_date", "first_service_source"),
            ):
                if (
                    selected.source == getattr(fact, source_name)
                    and selected.period == getattr(fact, month_name)
                    and selected.actual_date == getattr(fact, day_name)
                ):
                    actual_date_field = "tax_point_basis." + day_name
                    break
        intervals.append(
            TaxPointInterval(
                start,
                end,
                selected.period,
                selected.actual_date,
                selected.trigger,
                selected.source,
                actual_date_field,
            )
        )
    return TaxPointResolution(version.id, tuple(intervals), fact.service_pattern)


def validate_confirmed_tax_dates(fact):
    if fact.tax_obligation_date is not None and fact.tax_obligation_period is None:
        raise NeedsInformation(
            "tax_obligation_period",
            "实际税点日须同时有明确税所属月，不能从日期补造税月",
            sources=("original_document", "owner_confirmation"),
            precision=("month",),
        )
    if (
        fact.tax_obligation_date is not None
        and fact.tax_obligation_date.period != fact.tax_obligation_period
    ):
        raise KernelError(
            "tax_precision_conflict", "实际纳税义务日与税所属月不一致",
            fact_issues=[{
                "field": "tax_obligation_date", "message": "实际税点日须属于明确税所属月",
                "semantics": "accounting", "allowed_precision": ["day"],
                "reusable_sources": ["original_document", "owner_confirmation"],
            }],
        )


def tax_obligation_period_field():
    return Field(
        default=None, description="有来源明确确认的纳税义务所属月；未知时不从收入或收款月份推定",
        json_schema_extra={"x-accounting-fact": {
            "role": "accounting", "meaning": "confirmed_tax_obligation_period",
            "reusable_sources": ["original_document", "owner_confirmation", "tax_point_basis"],
            "constraint": "明确税所属月或有源税点依据；缺项不采用默认月份",
        }},
    )


def tax_obligation_date_field():
    return Field(
        default=None, description="有来源的实际税点日；可省略，不补月末；提供时须属于税所属月",
        json_schema_extra={"x-accounting-fact": {
            "role": "accounting", "meaning": "actual_tax_obligation_date",
            "reusable_sources": ["original_document", "owner_confirmation", "tax_point_basis"],
            "constraint": "仅在选择有效规则或核对实际先后需要日精度时必需；不推定实际日",
        }},
    )


def resolve_business_tax_point(version, ctx, gross_fen, *, as_of=None):
    """Use explicit sourced dates and verify any independently supplied basis."""
    fact = version.fact
    validate_confirmed_tax_dates(fact)
    bases = ctx.select(tax_basis_reads(fact.kind, version.subject_id)[0])
    if not bases:
        return None
    point = resolve_tax_point(
        ctx, fact.kind, version.subject_id, gross_fen, as_of or fact.period
    )
    if fact.tax_obligation_period is not None:
        if (sum_fen(i.gross_end_fen - i.gross_start_fen for i in point.intervals) != gross_fen
                or any(i.period != fact.tax_obligation_period for i in point.intervals)):
            raise NeedsInformation(
                "tax_obligation_period", "明确税所属月与另一税点依据的金额范围或月份不一致",
                sources=(version.subject_id, point.basis_id), precision=("month", "day"),
            )
        if fact.tax_obligation_date is not None and any(
            i.actual_date != fact.tax_obligation_date for i in point.intervals
        ):
            raise NeedsInformation(
                "tax_obligation_date", "明确实际税点日与另一税点依据不一致或依据仅有月精度",
                sources=(version.subject_id, point.basis_id), precision=("day",),
            )
    return point


def verify_inherited_advance_tax_point(version, ctx, source, advance_tax, earlier_used):
    fact = version.fact
    point = resolve_business_tax_point(version, ctx, fact.fulfilled_gross_fen)
    if fact.tax_obligation_period is None and point is None:
        return
    adopted = advance_tax or source
    public = adopted.values.get("tax_point_basis")
    evidence_sources = (source.subject_id, source.fact_id, adopted.id)
    if public is not None:
        intervals = public["intervals"]
        evidence_sources += (public["basis_fact_id"],)
    else:
        original = ctx.one("advance", f"@{fact.advance_id}")
        adopted_period = original.fact.tax_obligation_period
        if original.id != source.fact_id or adopted_period is None:
            raise NeedsInformation(
                "tax_obligation_period", "需复用原预收已采用的明确税点来源，不能补造税月",
                sources=evidence_sources, precision=("month", "day"),
            )
        intervals = [{
            "gross_start_fen": 0, "gross_end_fen": source.values["gross_fen"],
            "period": str(adopted_period), "actual_date": original.fact.tax_obligation_date,
        }]
    end = sum_fen((earlier_used, fact.fulfilled_gross_fen))
    known = [
        (max(i["gross_start_fen"], earlier_used) - earlier_used,
         min(i["gross_end_fen"], end) - earlier_used, i["period"], i["actual_date"])
        for i in intervals if i["gross_start_fen"] < end and i["gross_end_fen"] > earlier_used
    ]
    if sum_fen(right - left for left, right, _, _ in known) != fact.fulfilled_gross_fen:
        raise NeedsInformation("tax_point_basis", "已采用税点范围未完整覆盖本次履约",
                               sources=evidence_sources)
    if fact.tax_obligation_period is not None and any(
        month != fact.tax_obligation_period for _, _, month, _ in known
    ):
        raise NeedsInformation(
            "tax_obligation_period", "履约所填税月与原预收已采用税点不一致",
            sources=evidence_sources, precision=("month",),
        )
    if fact.tax_obligation_date is not None and any(
        day != fact.tax_obligation_date for _, _, _, day in known
    ):
        raise NeedsInformation(
            "tax_obligation_date", "履约实际税点日与已采用来源不一致或原来源仅有月精度",
            sources=evidence_sources, precision=("day",),
        )
    if point is None:
        return
    boundaries = sorted({0, fact.fulfilled_gross_fen,
        *(x for left, right, _, _ in known for x in (left, right)),
        *(x for i in point.intervals for x in (i.gross_start_fen, i.gross_end_fen))})
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        old = [i for i in known if i[0] <= left and i[1] >= right]
        new = [i for i in point.intervals if i.gross_start_fen <= left and i.gross_end_fen >= right]
        if (len(old) != 1 or len(new) != 1 or old[0][2] != new[0].period
                or new[0].actual_date is not None and old[0][3] != new[0].actual_date):
            raise NeedsInformation(
                "tax_point_basis", "履约独立税点依据与原预收已采用的完整范围、税月或实际日不一致",
                sources=(*evidence_sources, point.basis_id), precision=("month", "day"),
            )


def register(registry: Registry):
    registry.register(TaxPointBasis)
