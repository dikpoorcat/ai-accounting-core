"""Typed tax policies and deterministic integer-fen tax calculations.

Policies are input facts, never silently selected from the wall clock.  This
module has no persistence dependency and does not contain current statutory
rates: a published policy must carry its effective dates and official source.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from decimal import Context as DecimalContext
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from ai_accounting.policy_sources import OfficialPolicySourceURL

from ..contracts import (
    BalanceEffect,
    Context,
    Fact,
    FactVersion,
    KernelError,
    Line,
    NeedsInformation,
    Outcome,
    Read,
    Registry,
)
from ..types import (
    MAX_FEN,
    ActualDate,
    EvidenceDigest,
    Fen,
    NonNegativeFen,
    SubjectId,
    YearMonth,
    sum_fen,
)
from .money import ACTUAL_PAYMENT_KINDS
from . import income_tax
from .income_tax import IncomeTaxAssessment

Money = NonNegativeFen
VAT_CALCULATION_KINDS = (
    "service_sale",
    "sale_return",
    "asset_disposal",
    "advance",
    "advance_fulfillment",
    "service_tax_point",
    *ACTUAL_PAYMENT_KINDS,
    "advance_refund",
)


def _through_period(kind: str, key: str, period: YearMonth, *, lane="fact") -> Read:
    # Later confirmations validate their predecessors. An unrelated future use
    # must not invalidate an already frozen tax result merely for capacity checks.
    after = YearMonth.from_ordinal(period.ordinal + 1) if period != "9999-12" else None
    return Read(lane, kind, key, after)


class TaxInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def exact_decimal_rate(value) -> Decimal:
    """Convert the JSON decimal-string contract before strict nested validation."""
    if not isinstance(value, (Decimal, str)):
        raise ValueError("tax rates must be decimal strings or Decimal, never binary floats")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("tax rates must be valid finite decimals") from exc
    if not result.is_finite():
        raise ValueError("tax rates must be finite decimals")
    return result


class EffectiveTaxPolicy(TaxInput):
    version: str = Field(min_length=1, max_length=100)
    source_url: OfficialPolicySourceURL
    effective_from: date
    effective_to: date | None

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to precedes effective_from")
        return self

    def require_effective(self, start: date, end: date | None = None) -> None:
        end = end or start
        if (
            end < start
            or start < self.effective_from
            or (self.effective_to is not None and end > self.effective_to)
        ):
            raise ValueError("TAX_POLICY_NOT_EFFECTIVE_FOR_WHOLE_PERIOD")


class VatPolicy(EffectiveTaxPolicy):
    """Business-explicit VAT policy; percentages are decimal strings at the API."""

    rate_percent: Decimal = Field(ge=0, le=100)
    threshold_fen: Money
    threshold_operator: Literal["strictly_below", "at_or_below"]

    @field_validator("rate_percent", mode="before")
    @classmethod
    def no_binary_rate(cls, value):
        return exact_decimal_rate(value)


class SurtaxPolicy(EffectiveTaxPolicy):
    urban_rate_percent: Decimal = Field(ge=0, le=100)
    education_rate_percent: Decimal = Field(ge=0, le=100)
    local_education_rate_percent: Decimal = Field(ge=0, le=100)
    payable_fraction: Decimal = Field(ge=0, le=1)

    @field_validator(
        "urban_rate_percent",
        "education_rate_percent",
        "local_education_rate_percent",
        "payable_fraction",
        mode="before",
    )
    @classmethod
    def no_binary_rate(cls, value):
        return exact_decimal_rate(value)


class UsedAssetVatPolicy(EffectiveTaxPolicy):
    tax_base_rate_percent: Decimal = Field(ge=0, le=100)
    payable_rate_percent: Decimal = Field(ge=0, le=100)

    @field_validator("tax_base_rate_percent", "payable_rate_percent", mode="before")
    @classmethod
    def decimal_rates(cls, value):
        return exact_decimal_rate(value)


class VatSales(TaxInput):
    net_sales_fen: Fen
    accrued_vat_fen: Fen
    exemption_eligible: StrictBool
    filing_bucket: Literal["standard", "used_asset"] = "standard"
    rate_percent: Decimal | None = Field(default=None, ge=0, le=100)

    @field_validator("rate_percent", mode="before")
    @classmethod
    def declared_rate(cls, value):
        return None if value is None else exact_decimal_rate(value)


class VatAssessment(TaxInput):
    net_sales_fen: Money
    accrued_vat_fen: Money
    relief_fen: Money
    payable_vat_fen: Money
    urban_tax_fen: Money
    education_tax_fen: Money
    local_education_tax_fen: Money
    surtax_fen: Money
    vat_policy_version: str | None
    surtax_policy_version: str | None
    taxable_sales_fen: Money | None
    exempt_sales_fen: Money | None
    rounding_adjustment_fen: Fen
    filing_fields: dict[str, int]
    filing_mapping_version: str
    filing_source_url: str
    filing_effective_from: str
    filing_issues: tuple[dict[str, str], ...] = ()


def checked_fen(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_FEN:
        raise ValueError("FEN_OUT_OF_RANGE")
    return value


def round_fen(value: Decimal) -> int:
    with localcontext(DecimalContext(prec=80)):
        return checked_fen(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def split_tax_inclusive(gross_fen: int, rate_percent: Decimal) -> tuple[int, int]:
    checked_fen(gross_fen)
    if not isinstance(rate_percent, Decimal) or not rate_percent.is_finite():
        raise ValueError("INVALID_DECIMAL_RATE")
    if rate_percent < 0 or rate_percent > 100:
        raise ValueError("INVALID_DECIMAL_RATE")
    with localcontext(DecimalContext(prec=80)):
        tax = round_fen(Decimal(gross_fen) * rate_percent / (Decimal(100) + rate_percent))
    return gross_fen - tax, tax


def calculate_vat_period(
    *,
    sales: tuple[VatSales, ...],
    start: date,
    end: date,
    vat_policy: VatPolicy,
    surtax_policy: SurtaxPolicy,
) -> VatAssessment:
    vat_policy.require_effective(start, end)
    surtax_policy.require_effective(start, end)
    net = sum_fen(row.net_sales_fen for row in sales)
    accrued = sum_fen(row.accrued_vat_fen for row in sales)
    if net < 0 or accrued < 0:
        raise NeedsInformation(
            "tax_credit_disposition", "净退回超过本期销售，需要明确申报退抵税处理"
        )
    below = (
        net < vat_policy.threshold_fen
        if vat_policy.threshold_operator == "strictly_below"
        else net <= vat_policy.threshold_fen
    )
    relief = sum_fen(row.accrued_vat_fen for row in sales if row.exemption_eligible) if below else 0
    if not 0 <= relief <= accrued:
        raise NeedsInformation(
            "tax_credit_disposition", "退回与免税销售归属存在差异，需要明确原税期退抵及免税转回结果"
        )
    exempt_net = (
        sum_fen(row.net_sales_fen for row in sales if row.exemption_eligible) if below else 0
    )
    taxable_net = net - exempt_net
    standard_net = sum_fen(
        row.net_sales_fen for row in sales
        if row.filing_bucket == "standard" and not (below and row.exemption_eligible)
    )
    other_tax = sum_fen(
        row.accrued_vat_fen for row in sales
        if row.filing_bucket != "standard" and not (below and row.exemption_eligible)
    )
    if min(exempt_net, taxable_net, standard_net, other_tax) < 0:
        raise NeedsInformation("tax_credit_disposition", "各计税类别的净退回需要明确原税期处置")
    by_rate, exempt_by_rate = {}, {}
    for row in sales:
        if row.filing_bucket != "standard":
            continue
        rate = row.rate_percent if row.rate_percent is not None else vat_policy.rate_percent
        target = exempt_by_rate if below and row.exemption_eligible else by_rate
        target[rate] = sum_fen((target.get(rate, 0), row.net_sales_fen))
    if any(amount < 0 for amount in (*by_rate.values(), *exempt_by_rate.values())):
        raise NeedsInformation(
            "tax_credit_disposition", "不同征收率的退回须核对原税期来源，不互相抵销"
        )
    with localcontext(DecimalContext(prec=80)):
        normal_net = sum_fen(by_rate.get(rate, 0) for rate in (Decimal("1"), Decimal("3")))
        before_reduction = round_fen(Decimal(normal_net) * Decimal("3") / 100)
        before_reduction += sum_fen(
            round_fen(Decimal(amount) * rate / 100)
            for rate, amount in by_rate.items() if rate not in (Decimal("1"), Decimal("3"))
        )
        rate_reduction = round_fen(Decimal(by_rate.get(Decimal("1"), 0)) * Decimal("2") / 100)
        payable = checked_fen(before_reduction - rate_reduction + other_tax)
        exemption_display = round_fen(Decimal(sum_fen(
            exempt_by_rate.get(rate, 0) for rate in (Decimal("1"), Decimal("3"))
        )) * Decimal("3") / 100)
        exemption_display += sum_fen(
            round_fen(Decimal(amount) * rate / 100)
            for rate, amount in exempt_by_rate.items() if rate not in (Decimal("1"), Decimal("3"))
        )
    rounding = sum_fen((payable, relief, -accrued))
    issues = ()
    if (set(by_rate) | set(exempt_by_rate)) - {Decimal("1"), Decimal("3")} or any(
        row.filing_bucket != "standard" for row in sales
    ):
        issues = ({"field": "vat.form", "semantics": "accounting",
                   "message": "当前固定表式仅支持普通3%征收率及减按1%的业务，其他类别需对应表式"},)
    filing_fields = {
        "net_sales_fen": net,
        "taxable_sales_fen": taxable_net,
        "exempt_sales_fen": exempt_net,
        "row_1_fen": standard_net,
        "row_9_fen": exempt_net,
        "row_10_fen": exempt_net,
        "row_15_fen": before_reduction,
        "row_16_fen": rate_reduction,
        "row_17_fen": exemption_display,
        "row_18_fen": exemption_display,
        "row_20_fen": payable,
    }
    with localcontext(DecimalContext(prec=80)):

        def surcharge(rate: Decimal) -> int:
            return round_fen(Decimal(payable) * rate / 100 * surtax_policy.payable_fraction)

        urban = surcharge(surtax_policy.urban_rate_percent)
        education = surcharge(surtax_policy.education_rate_percent)
        local = surcharge(surtax_policy.local_education_rate_percent)
    return VatAssessment(
        net_sales_fen=net,
        accrued_vat_fen=accrued,
        relief_fen=relief,
        payable_vat_fen=payable,
        urban_tax_fen=urban,
        education_tax_fen=education,
        local_education_tax_fen=local,
        surtax_fen=checked_fen(urban + education + local),
        vat_policy_version=vat_policy.version,
        surtax_policy_version=surtax_policy.version,
        taxable_sales_fen=taxable_net,
        exempt_sales_fen=exempt_net,
        rounding_adjustment_fen=rounding,
        filing_fields=filing_fields,
        filing_mapping_version=("cn-small-scale-vat-2026-no6" if end >= date(2026, 2, 1)
                                else "cn-small-scale-vat-2021-no20"),
        filing_source_url=("https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247507/content.html"
                           if end >= date(2026, 2, 1)
                           else "https://12366.chinatax.gov.cn/bzds/003/003.html"),
        filing_effective_from="2026-02-01" if end >= date(2026, 2, 1) else "2021-08-01",
        filing_issues=issues + tuple(
            {"field": "vat.prepayment_basis", "amount_field": name, "message": message}
            for name, message in (
                ("row_21_fen", "第21栏缺少本期预缴税额依据，不能推定为零"),
                ("row_22_fen", "第22栏应补退税额需要第21栏预缴依据"),
                ("row_23_fen", "第23栏需要附列资料中已缴税额、减免及预缴依据"),
                ("row_24_fen", "第24栏需要附列资料中已缴费额、减免及预缴依据"),
                ("row_25_fen", "第25栏需要附列资料中已缴费额、减免及预缴依据"),
            )
        ),
    )


class VatPolicyFact(Fact):
    kind: ClassVar[str] = "vat_policy"
    policy: VatPolicy


class SurtaxPolicyFact(Fact):
    kind: ClassVar[str] = "surtax_policy"
    policy: SurtaxPolicy


class UsedAssetVatPolicyFact(Fact):
    kind: ClassVar[str] = "used_asset_vat_policy"
    policy: UsedAssetVatPolicy


def _declared_field(meaning: str, *, default=..., precision=("integer_fen",), **constraints):
    return Field(default=default, **constraints, json_schema_extra={"x-accounting-fact": {
        "role": "accounting", "meaning": meaning,
        "allowed_precision": list(precision),
        "reusable_sources": ["filed_tax_return", "tax_authority_assessment"],
        "constraint": "采用原件中同一税期、同一含义的实际值；明确零额有效，不用计算值补缺",
    }})


def declaration_scope(kind: str, subject_id: str) -> str:
    return f"tax-declaration:{kind}:{subject_id}"


class VatDeclarationActual(Fact):
    """Observed filed VAT and surtax amounts, independent of policy calculations."""

    kind: ClassVar[str] = "vat_declaration_actual"
    business_activity: ClassVar[bool] = False
    identity_fields: ClassVar[tuple[str, ...]] = ("assessment_id", "period_start", "period_end")
    assessment_id: SubjectId = _declared_field(
        "referenced_vat_and_surtax_assessment", precision=("subject_id",)
    )
    period_start: ActualDate = _declared_field("filed_tax_period_start", precision=("day",))
    period_end: ActualDate = _declared_field("filed_tax_period_end", precision=("day",))
    filing_evidence: EvidenceDigest = _declared_field(
        "actual_tax_original_evidence", precision=("evidence_digest",)
    )
    basis_type: Literal["filed_return", "tax_authority_assessment"] = _declared_field(
        "actual_tax_source_type", precision=("source_type",)
    )
    settlement_as_of: ActualDate | None = Field(
        default=None, description="原件应补退金额对应的结算核对日；未知不补日",
        json_schema_extra={"x-accounting-fact": {
            "role": "management", "meaning": "observed_tax_settlement_cutoff",
            "allowed_precision": ["day"], "reusable_sources": ["filed_tax_return"],
        }},
    )
    assessed_vat_fen: Money | None = _declared_field("actual_vat_row_20", default=None)
    urban_tax_fen: Money | None = _declared_field("actual_assessed_urban_tax", default=None)
    education_tax_fen: Money | None = _declared_field("actual_assessed_education_tax", default=None)
    local_education_tax_fen: Money | None = _declared_field(
        "actual_assessed_local_education_tax", default=None
    )
    prepaid_vat_fen: Money | None = _declared_field("actual_vat_row_21", default=None)
    vat_due_fen: Fen | None = _declared_field("actual_vat_row_22", default=None)
    urban_due_fen: Fen | None = _declared_field("actual_urban_tax_row_23", default=None)
    education_due_fen: Fen | None = _declared_field("actual_education_tax_row_24", default=None)
    local_education_due_fen: Fen | None = _declared_field(
        "actual_local_education_tax_row_25", default=None
    )
    row_15_fen: Money | None = _declared_field("actual_vat_row_15", default=None)
    row_16_fen: Money | None = _declared_field("actual_vat_row_16", default=None)
    row_17_fen: Money | None = _declared_field("actual_vat_row_17", default=None)
    row_18_fen: Money | None = _declared_field("actual_vat_row_18", default=None)
    exemption_relief_fen: Money | None = _declared_field(
        "original_accrued_vat_released_by_confirmed_exemption", default=None
    )

    @model_validator(mode="after")
    def filed_period(self):
        if self.period_start > self.period_end or self.period_end.period > self.period:
            raise ValueError("actual declaration must cover an elapsed tax period")
        if self.settlement_as_of is not None and self.settlement_as_of.period > self.period:
            raise ValueError("settlement cutoff cannot be after the observation month")
        return self

    def scopes(self):
        return (str(self.period), declaration_scope("tax_assessment", self.assessment_id))

    def reads(self):
        return (Read("fact", "tax_assessment", "@" + self.assessment_id),)


class IncomeTaxDeclarationActual(Fact):
    """Actual cumulative income tax; the amount due is a separate settlement field."""

    kind: ClassVar[str] = "income_tax_declaration_actual"
    business_activity: ClassVar[bool] = False
    identity_fields: ClassVar[tuple[str, ...]] = ("assessment_id", "year")
    assessment_id: SubjectId = _declared_field(
        "referenced_income_tax_assessment", precision=("subject_id",)
    )
    year: int = _declared_field("actual_income_tax_year", precision=("year",), ge=1, le=9999)
    tax_period_end: YearMonth = _declared_field(
        "actual_income_tax_coverage_end", precision=("month",)
    )
    filing_evidence: EvidenceDigest = _declared_field(
        "actual_tax_original_evidence", precision=("evidence_digest",)
    )
    basis_type: Literal["filed_return", "tax_authority_assessment"] = _declared_field(
        "actual_tax_source_type", precision=("source_type",)
    )
    settlement_as_of: ActualDate | None = Field(
        default=None, description="原件应补退金额对应的结算核对日；未知不补日",
        json_schema_extra={"x-accounting-fact": {
            "role": "management", "meaning": "observed_tax_settlement_cutoff",
            "allowed_precision": ["day"], "reusable_sources": ["filed_tax_return"],
        }},
    )
    cumulative_assessed_fen: Money | None = _declared_field(
        "actual_cumulative_assessed_income_tax", default=None
    )
    prepaid_tax_fen: Money | None = _declared_field(
        "actual_cumulative_prepaid_income_tax", default=None
    )
    tax_due_fen: Fen | None = _declared_field("actual_income_tax_due_or_refund", default=None)

    @model_validator(mode="after")
    def filed_period(self):
        if int(self.tax_period_end[:4]) != self.year or self.tax_period_end > self.period:
            raise ValueError("income tax coverage must belong to its year and precede observation")
        if self.settlement_as_of is not None and self.settlement_as_of.period > self.period:
            raise ValueError("settlement cutoff cannot be after the observation month")
        return self

    def scopes(self):
        return (str(self.period), declaration_scope("income_tax_assessment", self.assessment_id))

    def reads(self):
        return (Read("fact", "income_tax_assessment", "@" + self.assessment_id),)


def _actual_declaration(version, ctx, model):
    sources = ctx.facts(model.kind, declaration_scope(version.fact.kind, version.subject_id))
    if len(sources) > 1:
        raise NeedsInformation(
            model.kind, "同一核算事项存在多个实际申报来源，须明确唯一采用版本",
            sources=tuple(source.id for source in sources),
        )
    if not sources:
        return None
    source = sources[0]
    if source.fact.filing_evidence not in source.evidence:
        raise NeedsInformation(
            model.kind + ".filing_evidence", "实际申报原件须作为该事实的已登记证据",
            sources=(source.subject_id,), precision=("evidence_digest",),
        )
    return source


def _declared_amount(source, field):
    amount = getattr(source.fact, field)
    if amount is None:
        raise NeedsInformation(
            source.fact.kind + "." + field,
            "实际申报来源缺少该含义的税额，不能用计算值或应补额代替",
            sources=(source.subject_id, source.fact.filing_evidence), precision=("integer_fen",),
        )
    return amount


def _declaration_basis(source):
    return {"subject_id": source.subject_id, "fact_id": source.id,
            "filing_evidence": source.fact.filing_evidence, "basis_type": source.fact.basis_type}


def _actual_vat_assessment(source, fact, sales, calculated, vat_policy, surtax_policy):
    declared = source.fact
    for field in ("period_start", "period_end"):
        if getattr(declared, field) != getattr(fact, field):
            raise NeedsInformation(
                "vat_declaration_actual." + field, "实际申报税期与核算覆盖期不符",
                sources=(source.subject_id,), precision=("day",),
            )
    amounts = {name: _declared_amount(source, name) for name in (
        "assessed_vat_fen", "urban_tax_fen", "education_tax_fen", "local_education_tax_fen"
    )}
    vat = amounts["assessed_vat_fen"]
    if declared.prepaid_vat_fen is not None and declared.vat_due_fen is not None and (
        sum_fen((vat, -declared.prepaid_vat_fen)) != declared.vat_due_fen
    ):
        raise NeedsInformation("vat_declaration_actual.vat_due_fen",
                               "原件应纳、预缴与应补退金额不一致，须核对同含义字段",
                               sources=(source.subject_id,), precision=("integer_fen",))
    if declared.row_15_fen is not None and declared.row_16_fen is not None and (
        sum_fen((declared.row_15_fen, -declared.row_16_fen)) != vat
    ):
        raise NeedsInformation("vat_declaration_actual.row_15_fen",
                               "实际第15、16与20栏不一致，须核对同一申报原件",
                               sources=(source.subject_id,), precision=("integer_fen",))
    net = sum_fen(row.net_sales_fen for row in sales)
    accrued = sum_fen(row.accrued_vat_fen for row in sales)
    if min(net, accrued) < 0:
        raise NeedsInformation("tax_credit_disposition", "净退回仍须有明确的原税期退抵处理")
    # Only an explicitly identified exemption is classified as exemption income.
    # The remaining declaration variance is an adjustment, never inferred relief.
    relief = declared.exemption_relief_fen if declared.exemption_relief_fen is not None else 0
    if relief > accrued:
        raise NeedsInformation("vat_declaration_actual.exemption_relief_fen",
                               "实际免税转回不能超过原业务已计提税额",
                               sources=(source.subject_id,), precision=("integer_fen",))
    filing = {"net_sales_fen": net, "row_20_fen": vat}
    if calculated is not None:
        filing.update({name: amount for name, amount in calculated.filing_fields.items()
                       if name in {"taxable_sales_fen", "exempt_sales_fen", "row_1_fen",
                                   "row_9_fen", "row_10_fen"}})
    actual_fields = {"payable_vat_fen", "urban_tax_fen", "education_tax_fen",
                     "local_education_tax_fen", "surtax_fen", "row_20_fen"}
    optional = {"prepaid_vat_fen": "row_21_fen", "vat_due_fen": "row_22_fen",
                "urban_due_fen": "row_23_fen", "education_due_fen": "row_24_fen",
                "local_education_due_fen": "row_25_fen",
                **{f"row_{row}_fen": f"row_{row}_fen" for row in (15, 16, 17, 18)}}
    issues = []
    for name, target in optional.items():
        amount = getattr(declared, name)
        if amount is None:
            issues.append({"field": "vat_declaration_actual." + name, "amount_field": target,
                           "message": "实际来源未明确该申报栏，不用计算值补缺"})
        else:
            filing[target] = amount
            actual_fields.add(target)
    if declared.exemption_relief_fen is not None:
        actual_fields.add("relief_fen")
    end = date.fromisoformat(fact.period_end)
    assessment = VatAssessment(
        net_sales_fen=net, accrued_vat_fen=accrued, relief_fen=relief,
        payable_vat_fen=vat, **{name: amounts[name] for name in (
            "urban_tax_fen", "education_tax_fen", "local_education_tax_fen"
        )}, surtax_fen=sum_fen(amounts[name] for name in (
            "urban_tax_fen", "education_tax_fen", "local_education_tax_fen"
        )), vat_policy_version=vat_policy.version if vat_policy is not None else None,
        surtax_policy_version=surtax_policy.version if surtax_policy is not None else None,
        taxable_sales_fen=calculated.taxable_sales_fen if calculated is not None else None,
        exempt_sales_fen=calculated.exempt_sales_fen if calculated is not None else None,
        rounding_adjustment_fen=sum_fen((vat, relief, -accrued)), filing_fields=filing,
        filing_mapping_version=("cn-small-scale-vat-2026-no6" if end >= date(2026, 2, 1)
                                else "cn-small-scale-vat-2021-no20"),
        filing_source_url=("https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247507/content.html"
                           if end >= date(2026, 2, 1)
                           else "https://12366.chinatax.gov.cn/bzds/003/003.html"),
        filing_effective_from="2026-02-01" if end >= date(2026, 2, 1) else "2021-08-01",
        filing_issues=tuple(issues),
    )
    return assessment, sorted(actual_fields)


class TaxAssessment(Fact):
    kind: ClassVar[str] = "tax_assessment"
    identity_fields: ClassVar[tuple[str, ...]] = ("period_start", "period_end")
    period_start: ActualDate
    period_end: ActualDate
    vat_policy_id: SubjectId
    surtax_policy_id: SubjectId
    tax_credit_ids: tuple[SubjectId, ...] = ()

    @model_validator(mode="after")
    def valid_period(self):
        if len(set(self.tax_credit_ids)) != len(self.tax_credit_ids):
            raise ValueError("tax credits cannot be applied twice")
        if self.period_end < self.period_start or self.period_end.period > self.period:
            raise ValueError("tax interval must end by its accounting period")
        start, end = date.fromisoformat(self.period_start), date.fromisoformat(self.period_end)
        if start.day != 1 or end.day != calendar.monthrange(end.year, end.month)[1]:
            raise ValueError("tax assessments use complete calendar months")
        if start.year != end.year:
            raise ValueError("tax assessment must stay within one calendar year")
        return self

    def scopes(self):
        return (
            str(self.period),
            *(f"tax-assessment:{month}" for month in self.tax_months()),
            *(f"tax-credit-use:{source}" for source in self.tax_credit_ids),
        )

    def tax_months(self):
        return tuple(
            YearMonth.from_ordinal(i)
            for i in range(self.period_start.period.ordinal, self.period_end.period.ordinal + 1)
        )

    def reads(self):
        return (
            Read("fact", "vat_policy", f"@{self.vat_policy_id}"),
            Read("fact", "surtax_policy", f"@{self.surtax_policy_id}"),
            *(Read("fact", self.kind, f"tax-assessment:{month}") for month in self.tax_months()),
            *(Read("calculation", "tax_credit_confirmation", f"@{x}") for x in self.tax_credit_ids),
            *(
                _through_period(self.kind, f"tax-credit-use:{x}", self.period)
                for x in self.tax_credit_ids
            ),
            *(
                Read("calculation", kind, f"tax:{month}")
                for month in self.tax_months()
                for kind in VAT_CALCULATION_KINDS
            ),
        )

    def reads_for(self, subject_id):
        return self.reads() + (Read("fact", VatDeclarationActual.kind,
                                    declaration_scope(self.kind, subject_id)),)

def calculate_tax_assessment(version: FactVersion, ctx: Context) -> Outcome:
    fact: TaxAssessment = version.fact
    actual = _actual_declaration(version, ctx, VatDeclarationActual)
    if actual is None:
        vat_version = ctx.one("vat_policy", f"@{fact.vat_policy_id}")
        surtax_version = ctx.one("surtax_policy", f"@{fact.surtax_policy_id}")
    else:
        vat_sources = ctx.facts("vat_policy", f"@{fact.vat_policy_id}")
        surtax_sources = ctx.facts("surtax_policy", f"@{fact.surtax_policy_id}")
        vat_version = vat_sources[0] if vat_sources else None
        surtax_version = surtax_sources[0] if surtax_sources else None
    vat_policy = vat_version.fact.policy if vat_version is not None else None
    surtax_policy = surtax_version.fact.policy if surtax_version is not None else None
    for month in fact.tax_months():
        if any(
            other.subject_id != version.subject_id
            for other in ctx.facts(fact.kind, f"tax-assessment:{month}")
        ):
            raise KernelError("overlapping_tax_assessments", "同一税务期间不能重复确认")
    credits, credited_returns = [], set()
    for credit_id in fact.tax_credit_ids:
        credit = _tax_calculation(ctx, "tax_credit_confirmation", credit_id)
        if credit.period > fact.period:
            raise KernelError("tax_credit_future_confirmation", "退抵税不能先于已确认结果入账")
        if any(
            other.subject_id != version.subject_id
            for other in ctx.select(
                _through_period(
                    fact.kind,
                    f"tax-credit-use:{credit_id}",
                    fact.period,
                )
            )
        ):
            raise KernelError("tax_credit_already_applied", "同一退抵税确认只能归入一个税期")
        for item in credit.values["returns"]:
            if not fact.period_start.period <= item["period"] <= fact.period_end.period:
                raise KernelError("tax_credit_return_period", "退抵税来源必须覆盖本税期实际退回")
            identity = (item["return_kind"], item["return_id"])
            if identity in credited_returns:
                raise KernelError("duplicate_tax_credit_return", "同一退回不能重复生成退抵税债权")
            credited_returns.add(identity)
        credits.append(credit)
    sales = []
    source_calculation_ids = set()
    for index in range(fact.period_start.period.ordinal, fact.period_end.period.ordinal + 1):
        month = YearMonth.from_ordinal(index)
        for kind in VAT_CALCULATION_KINDS:
            for row in ctx.calculations(kind, f"tax:{month}"):
                source_calculation_ids.add(row.id)
                if kind in ACTUAL_PAYMENT_KINDS:
                    for item in row.values.get("tax_sales", ()):
                        if actual is None and item.get("rate_percent") is None and (
                            item["net_sales_fen"] or item["accrued_vat_fen"]
                        ):
                            raise NeedsInformation(
                                "tax_source.rate_percent", "计税来源缺少明确征收率",
                                sources=(row.subject_id,),
                            )
                    sales.extend(
                        VatSales(
                            **{
                                field: item[field]
                                for field in (
                                    "net_sales_fen",
                                    "accrued_vat_fen",
                                    "exemption_eligible",
                                )
                            } | {"rate_percent": item.get("rate_percent")}
                        )
                        for item in row.values.get("tax_sales", ())
                    )
                    continue
                if kind in {"sale_return", "advance_refund"}:
                    if (kind, row.subject_id) in credited_returns:
                        continue
                    original_period = row.values.get("original_sale_period")
                    if original_period is not None and not (
                        fact.period_start.period <= original_period <= fact.period_end.period
                    ):
                        raise NeedsInformation(
                            "tax_credit_disposition",
                            "跨税期退回需要原申报和税务确认的退抵结果",
                            sources=(row.subject_id,),
                        )
                if (actual is None and kind != "asset_disposal"
                        and row.values.get("rate_percent") is None
                        and (row.values["net_sales_fen"] or row.values["accrued_vat_fen"])
                ):
                    raise NeedsInformation(
                        "tax_source.rate_percent", "计税来源缺少明确征收率",
                        sources=(row.subject_id,),
                    )
                sales.append(
                    VatSales(
                        net_sales_fen=row.values["net_sales_fen"],
                        accrued_vat_fen=row.values["accrued_vat_fen"],
                        exemption_eligible=row.values["exemption_eligible"],
                        filing_bucket="used_asset" if kind == "asset_disposal" else "standard",
                        rate_percent=row.values.get("rate_percent"),
                    )
                )
    calculated, reference_issue = None, None
    if actual is not None:
        # Validate the authoritative source before attempting optional reference arithmetic.
        _actual_vat_assessment(actual, fact, sales, None,
                               vat_policy, surtax_policy)
    try:
        if vat_policy is None or surtax_policy is None:
            raise NeedsInformation(
                "calculation_policy", "计算参考缺少政策来源，不影响已明确实际税额"
            )
        if actual is not None and any(row.rate_percent is None and (
            row.net_sales_fen or row.accrued_vat_fen
        ) and row.filing_bucket == "standard" for row in sales):
            raise NeedsInformation("tax_source.rate_percent",
                                   "计算参考缺少来源征收率，不采用推断性政策默认值")
        calculated = calculate_vat_period(
            sales=tuple(sales), start=date.fromisoformat(fact.period_start),
            end=date.fromisoformat(fact.period_end), vat_policy=vat_policy,
            surtax_policy=surtax_policy,
        )
    except (NeedsInformation, ValueError) as error:
        if actual is None:
            raise
        reference_issue = (
            error.response() if isinstance(error, KernelError) else {"message": str(error)}
        )
    if actual is not None:
        assessment, actual_fields = _actual_vat_assessment(
            actual, fact, sales, calculated, vat_policy, surtax_policy
        )
    else:
        assessment, actual_fields = calculated, []
    lines = []
    exemption_profit_lines = []
    if assessment.rounding_adjustment_fen > 0:
        lines += [Line("571105", debit=assessment.rounding_adjustment_fen),
                  Line("222101", credit=assessment.rounding_adjustment_fen)]
    elif assessment.rounding_adjustment_fen < 0:
        lines += [Line("222101", debit=-assessment.rounding_adjustment_fen),
                  Line("6301", credit=-assessment.rounding_adjustment_fen)]
    if assessment.relief_fen:
        exemption_profit_lines.append({"line_no": len(lines) + 2,
                                       "amount_fen": assessment.relief_fen})
        lines += [
            Line("222101", debit=assessment.relief_fen),
            Line("6301", credit=assessment.relief_fen),
        ]
    if assessment.surtax_fen:
        lines += [
            Line("5403", debit=assessment.surtax_fen),
            Line("222102", credit=assessment.surtax_fen),
        ]
    obligations = []
    for name, amount, account in (
        ("vat", assessment.payable_vat_fen, "222101"),
        ("surtax", assessment.surtax_fen, "222102"),
    ):
        if amount or actual is not None:
            obligations.append(
                {
                    "name": name,
                    "key": f"tax_assessment:{version.subject_id}:{name}",
                    "amount_fen": amount,
                    "account": account,
                    "normal": "credit",
                    "category": "payable",
                    "counterparty_id": None,
                    "cashflow": "tax_payments",
                }
            )
    for credit in credits:
        reversed_relief = credit.values["exemption_reversal_fen"]
        if reversed_relief:
            exemption_profit_lines.append({"line_no": len(lines) + 1,
                                           "amount_fen": -reversed_relief})
            lines += [Line("6301", debit=reversed_relief), Line("222101", credit=reversed_relief)]
        surtax_credit = credit.values["surtax_credit_fen"]
        if surtax_credit:
            lines += [Line("222102", debit=surtax_credit), Line("5403", credit=surtax_credit)]
        for tax_name, amount, account in (
            ("vat", credit.values["vat_credit_fen"], "222101"),
            ("surtax", surtax_credit, "222102"),
        ):
            if not amount:
                continue
            name = f"{tax_name}_credit_{credit.subject_id}"
            obligations.append(
                {
                    "name": name,
                    "key": f"tax_assessment:{version.subject_id}:{name}",
                    "amount_fen": amount,
                    "account": account,
                    "normal": "debit",
                    "category": "receivable",
                    "counterparty_id": None,
                    "cashflow": "tax_refunds",
                    "settlement_modes": credit.values["settlement_modes"],
                }
            )
    return Outcome(
        tuple(lines),
        assessment.model_dump(mode="json")
        | {
            "obligations": obligations,
            "tax_credit_calculations": [credit.id for credit in credits],
            "vat_source_calculation_ids": sorted(source_calculation_ids),
            "period_start": str(fact.period_start),
            "period_end": str(fact.period_end),
            "vat_exemption_profit_lines": exemption_profit_lines,
            "amount_source": "actual" if actual is not None else "provisional",
            "actual_declaration": _declaration_basis(actual) if actual is not None else None,
            "actual_fields": actual_fields,
            "calculated_assessment": calculated.model_dump(mode="json")
            if calculated is not None else None,
            "calculation_reference_issue": reference_issue,
            "declaration_adjustment_fen": sum_fen((assessment.payable_vat_fen,
                                                   -calculated.payable_vat_fen))
            if actual is not None and calculated is not None else None,
            "exemption_relief_confirmed": (
                actual is None or actual.fact.exemption_relief_fen is not None
            ),
        },
        tuple(BalanceEffect(o["key"], o["amount_fen"], o["category"]) for o in obligations),
        ({"vat_policy_id": vat_version.id if vat_version is not None else None,
          "surtax_policy_id": surtax_version.id if surtax_version is not None else None},),
    )


class HistoricalIncomeTaxAssessment(Fact):
    """Confirmed cumulative enterprise-income-tax assessment, not an inferred rate."""

    kind: ClassVar[str] = "income_tax_assessment"
    identity_fields: ClassVar[tuple[str, ...]] = ("year",)
    cumulative_assessed_fen: Money
    assessment_basis: Literal["confirmed_provision", "filed_return", "annual_settlement"]
    year: int = Field(ge=1, le=9999)

    @model_validator(mode="after")
    def tax_year(self):
        period_year = int(self.period[:4])
        if period_year < self.year or (
            period_year != self.year and self.assessment_basis != "annual_settlement"
        ):
            raise ValueError("income-tax assessment period must belong to the tax year")
        return self

    def scopes(self):
        return (
            str(self.period),
            f"income-tax:{self.year}",
            f"income-tax:{self.year}:{self.period}",
        )

    def reads(self):
        return (
            Read("fact", self.kind, f"income-tax:{self.year}:{self.period}"),
            Read("calculation", self.kind, f"income-tax:{self.year}", self.period),
        )

    def reads_for(self, subject_id):
        return self.reads() + (
            Read("fact", IncomeTaxDeclarationActual.kind, declaration_scope(self.kind, subject_id)),
        )

def calculate_income_tax_assessment(version: FactVersion, ctx: Context) -> Outcome:
    fact: IncomeTaxAssessment = version.fact
    actual = _actual_declaration(version, ctx, IncomeTaxDeclarationActual)
    tax_period_end = (
        YearMonth(f"{fact.year:04d}-12")
        if fact.assessment_basis == "annual_settlement" else fact.period
    )
    calculated = None
    reference_issue = None
    cumulative = fact.cumulative_assessed_fen
    if actual is not None:
        if actual.fact.year != fact.year or actual.fact.tax_period_end != tax_period_end:
            raise NeedsInformation("income_tax_declaration_actual.tax_period_end",
                                   "实际所得税来源的年度或覆盖税期与核算事项不符",
                                   sources=(actual.subject_id,), precision=("month",))
        cumulative = _declared_amount(actual, "cumulative_assessed_fen")
        if actual.fact.prepaid_tax_fen is not None and actual.fact.tax_due_fen is not None and (
            sum_fen((cumulative, -actual.fact.prepaid_tax_fen)) != actual.fact.tax_due_fen
        ):
            raise NeedsInformation("income_tax_declaration_actual.tax_due_fen",
                                   "实际累计应纳、预缴与应补退金额不一致，须核对原件字段",
                                   sources=(actual.subject_id,), precision=("integer_fen",))
    if fact.assessment_basis == "computed_provision":
        try:
            calculated = income_tax.calculate_computed_provision(version, ctx)
        except NeedsInformation as error:
            if actual is None:
                raise
            reference_issue = error.response()
        if actual is None:
            cumulative = calculated["cumulative_assessed_fen"]
    else:
        calculated = {"cumulative_assessed_fen": fact.cumulative_assessed_fen}
    if cumulative is None:
        raise NeedsInformation("cumulative_assessed_fen", "需要明确的累计所得税确认额",
                               precision=("integer_fen",))
    earlier = ctx.select(Read("calculation", fact.kind, f"income-tax:{fact.year}", fact.period))
    same_month = [
        item
        for item in ctx.facts(fact.kind, f"income-tax:{fact.year}:{fact.period}")
        if item.subject_id != version.subject_id
    ]
    if same_month:
        raise KernelError("duplicate_tax_assessment", "同月所得税采用原身份修订，不重复计提")
    previous = max(earlier, key=lambda item: item.period, default=None)
    opening = previous.values["cumulative_assessed_fen"] if previous else 0
    if calculated is not None:
        calculated = calculated | {
            "change_fen": sum_fen((calculated["cumulative_assessed_fen"], -opening)),
        }
    difference = sum_fen((cumulative, -opening))
    if difference > 0:
        lines = (Line("5801", debit=difference), Line("222106", credit=difference))
    elif difference < 0:
        lines = (Line("222106", debit=-difference), Line("5801", credit=-difference))
    else:
        lines = ()
    obligation = {
        "name": "tax",
        "key": f"income_tax_assessment:{version.subject_id}:tax",
        "amount_fen": abs(difference),
        "account": "222106",
        "normal": "credit" if difference >= 0 else "debit",
        "category": "payable" if difference >= 0 else "receivable",
        "counterparty_id": None,
        "cashflow": "tax_payments",
    }
    return Outcome(
        lines,
        {
            "cumulative_assessed_fen": cumulative,
            "assessment_basis": fact.assessment_basis,
            "year": fact.year,
            "tax_period_end": str(tax_period_end),
            "change_fen": difference,
            "obligations": [obligation],
            "amount_source": "actual" if actual is not None else "provisional",
            "actual_declaration": _declaration_basis(actual) if actual is not None else None,
            "calculated_assessment": calculated,
            **({"calculation_reference_issue": reference_issue} if reference_issue else {}),
            **({"prepaid_tax_fen": actual.fact.prepaid_tax_fen,
                "tax_due_fen": actual.fact.tax_due_fen,
            } if actual is not None else {}),
        },
        (BalanceEffect(obligation["key"], abs(difference), obligation["category"]),),
    )


class TaxCreditReturn(TaxInput):
    """A credit-note source, explicitly attributed to an originally filed sale."""

    return_id: SubjectId
    original_sale_id: SubjectId
    return_kind: ClassVar[str] = "sale_return"
    original_kind: ClassVar[str] = "service_sale"

    @property
    def original_id(self):
        return self.original_sale_id


class TaxCreditAdvanceReturn(TaxInput):
    """An explicitly selected refund of an originally taxed customer advance."""

    return_id: SubjectId
    original_advance_id: SubjectId
    return_kind: ClassVar[str] = "advance_refund"
    original_kind: ClassVar[str] = "advance"

    @property
    def original_id(self):
        return self.original_advance_id


class TaxCreditConfirmation(Fact):
    """Recorded tax-authority result, never a tax filing or a refund application.

    This fact authorizes moving its attributed returns out of the current-period
    threshold calculation and into the original filed period's confirmed credit.
    No receivable or cash is posted until a TaxAssessment explicitly applies it.
    The confirmation and original filing evidence must both be registered.
    """

    kind: ClassVar[str] = "tax_credit_confirmation"
    immutable: ClassVar[bool] = True
    original_assessment_id: SubjectId
    original_period_start: ActualDate
    original_period_end: ActualDate
    returns: tuple[TaxCreditReturn | TaxCreditAdvanceReturn, ...] = Field(min_length=1)
    authority_confirmed: StrictBool | None = None
    original_filing_reference: str | None = Field(default=None, min_length=1)
    confirmation_reference: str | None = Field(default=None, min_length=1)
    original_filing_evidence: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_evidence: EvidenceDigest | None = None
    confirmation_date: ActualDate | None = None
    disposition: Literal["refund", "offset", "refund_or_offset"] | None = None
    vat_credit_fen: Money | None = None
    surtax_credit_fen: Money | None = None
    exemption_reversal_fen: Money | None = None

    @model_validator(mode="after")
    def original_period(self):
        if self.original_period_end < self.original_period_start:
            raise ValueError("original tax interval is reversed")
        if self.original_period_end.period >= self.period:
            raise ValueError("a prior-period tax credit must refer to an earlier tax period")
        if self.confirmation_date is not None and self.confirmation_date.period > self.period:
            raise ValueError("tax confirmation cannot post before the authority confirmation")
        if len({item.return_id for item in self.returns}) != len(self.returns):
            raise ValueError("duplicate return source")
        return self

    def scopes(self):
        return (
            str(self.period),
            f"tax-credit-origin:{self.original_assessment_id}",
            *(f"tax-credit-return:{item.return_id}" for item in self.returns),
        )

    def reads(self):
        return tuple(
            sorted(
                {
                    Read("calculation", "tax_assessment", f"@{self.original_assessment_id}"),
                    _through_period(
                        self.kind,
                        f"tax-credit-origin:{self.original_assessment_id}",
                        self.period,
                    ),
                    *(
                        _through_period(self.kind, f"tax-credit-return:{x.return_id}", self.period)
                        for x in self.returns
                    ),
                    *(Read("fact", x.return_kind, f"@{x.return_id}") for x in self.returns),
                    *(Read("calculation", x.return_kind, f"@{x.return_id}") for x in self.returns),
                    *(
                        Read("calculation", x.original_kind, f"@{x.original_id}")
                        for x in self.returns
                    ),
                    *(
                        _through_period(
                            "service_tax_point", f"advance-tax:{x.original_id}",
                            self.original_period_end.period,
                            lane="calculation",
                        )
                        for x in self.returns if x.original_kind == "advance"
                    ),
                }
            )
        )


def _tax_calculation(ctx: Context, kind: str, subject: str):
    rows = ctx.calculations(kind, f"@{subject}")
    if len(rows) != 1:
        raise NeedsInformation(kind, "需要已发布且唯一的税务来源", sources=(subject,))
    return rows[0]


def calculate_tax_credit_confirmation(version: FactVersion, ctx: Context) -> Outcome:
    fact: TaxCreditConfirmation = version.fact
    if fact.authority_confirmed is not True:
        raise NeedsInformation("authority_confirmed", "须明确税务机关已确认原申报的退抵结果")
    for field in (
        "original_filing_reference",
        "confirmation_reference",
        "original_filing_evidence",
        "confirmation_evidence",
        "confirmation_date",
        "disposition",
        "vat_credit_fen",
        "surtax_credit_fen",
        "exemption_reversal_fen",
    ):
        if getattr(fact, field) is None:
            raise NeedsInformation(field, "退抵税须有明确结果，不能推定金额、日期或权利")
    for field in ("original_filing_evidence", "confirmation_evidence"):
        if getattr(fact, field) not in version.evidence:
            raise NeedsInformation(field, "原申报和税务确认凭据须作为本事实的已登记证据")
    original = _tax_calculation(ctx, "tax_assessment", fact.original_assessment_id)
    if original.values.get("exemption_relief_confirmed") is False:
        raise NeedsInformation(
            "original_exemption_relief", "原实际税额来源未明确免税转回，不能推定退抵税归属",
            sources=(fact.original_assessment_id,), precision=("integer_fen",),
        )
    if (
        original.values.get("period_start") != fact.original_period_start
        or original.values.get("period_end") != fact.original_period_end
    ):
        raise KernelError("tax_credit_original_period", "原申报来源与明确原税期不一致")
    returned_vat, previously_exempt, returns = 0, 0, []
    for item in fact.returns:
        returned_fact = ctx.one(item.return_kind, f"@{item.return_id}")
        returned = _tax_calculation(ctx, item.return_kind, item.return_id)
        sale = _tax_calculation(ctx, item.original_kind, item.original_id)
        if item.original_kind == "advance" and not sale.values["vat_due_on_advance"]:
            tax_sources = ctx.select(_through_period(
                "service_tax_point", f"advance-tax:{item.original_id}",
                fact.original_period_end.period,
                lane="calculation",
            ))
            matches = [source for source in tax_sources
                       if source.values.get("advance_id") == item.original_id
                       and source.id == returned.values.get("tax_source_calculation")]
            if len(matches) != 1:
                raise NeedsInformation(
                    "original_tax_source", "预收退款需要原已采用转税结果的精确来源",
                    sources=(item.original_id, item.return_id),
                )
            sale = matches[0]
            if sale.id not in original.values.get("vat_source_calculation_ids", ()):
                raise NeedsInformation(
                    "original_filing_tax_source", "原申报未建立该预收转税结果的精确采用依据",
                    sources=(fact.original_assessment_id, sale.subject_id),
                )
        linked_source = (
            returned_fact.fact.sale_id
            if item.return_kind == "sale_return"
            else returned_fact.fact.advance_id
        )
        if linked_source != item.original_id or not (
            fact.original_period_start.period
            <= (returned.values.get("original_sale_period") or "")
            <= fact.original_period_end.period
        ):
            raise KernelError("tax_credit_return_source", "退回必须来自明确原申报税期的销售")
        if returned.period <= fact.original_period_end.period or returned.period > fact.period:
            raise KernelError("tax_credit_return_date", "退回须晚于原税期且不晚于确认所属期")
        if fact.confirmation_date.period < returned.period:
            raise KernelError("tax_credit_confirmation_date", "税务确认不能早于其退回来源月份")
        if any(
            other.subject_id != version.subject_id
            for other in ctx.select(
                _through_period(
                    fact.kind,
                    f"tax-credit-return:{item.return_id}",
                    fact.period,
                )
            )
        ):
            raise KernelError("duplicate_tax_credit_return", "退回已有退抵税确认，不得重复确认")
        amount = -returned.values["accrued_vat_fen"]
        returned_vat = sum_fen((returned_vat, checked_fen(amount)))
        if (original.values.get("amount_source") != "actual"
                and original.values["relief_fen"] and sale.values["exemption_eligible"]):
            previously_exempt = sum_fen((previously_exempt, amount))
        returns.append(
            {
                "return_id": item.return_id,
                "return_kind": item.return_kind,
                "calculation_id": returned.id,
                "period": str(returned.period),
                "original_sale_calculation": sale.id,
            }
        )
    if original.values.get("amount_source") == "actual":
        # The authority-confirmed reversal is the actual classification. A policy
        # eligibility flag cannot reconstruct an actual filing's partial exemption.
        previously_exempt = fact.exemption_reversal_fen
        other_reversals = [
            peer.fact.exemption_reversal_fen for peer in ctx.select(
                _through_period(
                    fact.kind, f"tax-credit-origin:{fact.original_assessment_id}", fact.period
                )
            ) if peer.subject_id != version.subject_id
        ]
        if any(amount is None for amount in other_reversals):
            raise NeedsInformation("exemption_reversal_fen", "同原申报其他确认缺少实际免税转回金额")
        if sum_fen((fact.exemption_reversal_fen, *other_reversals)) > original.values["relief_fen"]:
            raise KernelError("tax_credit_exceeds_exemption", "累计免税转回不能超过原实际免税金额")
    if (
        fact.exemption_reversal_fen != previously_exempt
        or sum_fen(
            (
                fact.vat_credit_fen,
                fact.exemption_reversal_fen,
            )
        )
        != returned_vat
    ):
        raise KernelError(
            "tax_credit_amount_conflict", "已确认增值税退抵与原免税转回合计须核对红字税额及原申报"
        )
    peers = [
        other.fact
        for other in ctx.select(
            _through_period(
                fact.kind,
                f"tax-credit-origin:{fact.original_assessment_id}",
                fact.period,
            )
        )
        if other.subject_id != version.subject_id
    ]
    for field, capacity_field in (
        ("vat_credit_fen", "payable_vat_fen"),
        ("surtax_credit_fen", "surtax_fen"),
    ):
        values = [getattr(peer, field) for peer in peers]
        if any(amount is None for amount in values):
            raise NeedsInformation(field, "同原税期其他退抵确认金额尚不完整")
        if sum_fen((getattr(fact, field), *values)) > original.values[capacity_field]:
            raise KernelError("tax_credit_exceeds_filing", "累计退抵税额不得超过原已申报税额")
    return Outcome(
        (),
        {
            "original_assessment_calculation": original.id,
            "original_filing_reference": fact.original_filing_reference,
            "confirmation_reference": fact.confirmation_reference,
            "returns": returns,
            "vat_credit_fen": fact.vat_credit_fen,
            "surtax_credit_fen": fact.surtax_credit_fen,
            "exemption_reversal_fen": fact.exemption_reversal_fen,
            "settlement_modes": {
                "refund": ["payment"],
                "offset": ["offset"],
                "refund_or_offset": ["payment", "offset"],
            }[fact.disposition],
        },
        explanation=({"evidence": list(version.evidence)},),
    )


def register(registry: Registry) -> None:
    registry.register(VatPolicyFact)
    registry.register(SurtaxPolicyFact)
    registry.register(UsedAssetVatPolicyFact)
    registry.register(VatDeclarationActual)
    registry.register(IncomeTaxDeclarationActual)
    income_tax.register(registry)
    registry.register(TaxAssessment, calculate_tax_assessment)
    registry.register(
        IncomeTaxAssessment, calculate_income_tax_assessment,
        table="fact_income_tax_assessment_computed",
        historical=((HistoricalIncomeTaxAssessment, "fact_income_tax_assessment"),),
    )
    registry.register(TaxCreditConfirmation, calculate_tax_credit_confirmation)
