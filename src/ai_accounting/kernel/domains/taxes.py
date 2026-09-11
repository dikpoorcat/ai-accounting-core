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
from typing import Annotated, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

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
from ..types import MAX_FEN, ActualDate, Fen, NonNegativeFen, YearMonth, sum_fen
from .money import ACTUAL_PAYMENT_KINDS

Money = NonNegativeFen
TaxSourceId = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
]
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


def _through_period(kind: str, key: str, period: YearMonth) -> Read:
    # Later confirmations validate their predecessors. An unrelated future use
    # must not invalidate an already frozen tax result merely for capacity checks.
    after = YearMonth.from_ordinal(period.ordinal + 1) if period != "9999-12" else None
    return Read("fact", kind, key, after)


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
    source_url: str
    effective_from: date
    effective_to: date | None

    @field_validator("source_url")
    @classmethod
    def official_source(cls, value: str) -> str:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if parsed.scheme != "https" or not hostname.endswith(".gov.cn"):
            raise ValueError("a primary official HTTPS .gov.cn tax policy source is required")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("policy source must not contain credentials")
        return value

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


class VatAssessment(TaxInput):
    net_sales_fen: Money
    accrued_vat_fen: Money
    relief_fen: Money
    payable_vat_fen: Money
    urban_tax_fen: Money
    education_tax_fen: Money
    local_education_tax_fen: Money
    surtax_fen: Money
    vat_policy_version: str
    surtax_policy_version: str


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
    payable = accrued - relief
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


class TaxAssessment(Fact):
    kind: ClassVar[str] = "tax_assessment"
    identity_fields: ClassVar[tuple[str, ...]] = ("period_start", "period_end")
    period_start: ActualDate
    period_end: ActualDate
    vat_policy_id: str = Field(min_length=1)
    surtax_policy_id: str = Field(min_length=1)
    tax_credit_ids: tuple[TaxSourceId, ...] = ()

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


def calculate_tax_assessment(version: FactVersion, ctx: Context) -> Outcome:
    fact: TaxAssessment = version.fact
    vat_version = ctx.one("vat_policy", f"@{fact.vat_policy_id}")
    surtax_version = ctx.one("surtax_policy", f"@{fact.surtax_policy_id}")
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
    for index in range(fact.period_start.period.ordinal, fact.period_end.period.ordinal + 1):
        month = YearMonth.from_ordinal(index)
        for kind in VAT_CALCULATION_KINDS:
            for row in ctx.calculations(kind, f"tax:{month}"):
                if kind in ACTUAL_PAYMENT_KINDS:
                    sales.extend(
                        VatSales(
                            **{
                                field: item[field]
                                for field in (
                                    "net_sales_fen",
                                    "accrued_vat_fen",
                                    "exemption_eligible",
                                )
                            }
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
                sales.append(
                    VatSales(
                        net_sales_fen=row.values["net_sales_fen"],
                        accrued_vat_fen=row.values["accrued_vat_fen"],
                        exemption_eligible=row.values["exemption_eligible"],
                    )
                )
    assessment = calculate_vat_period(
        sales=tuple(sales),
        start=date.fromisoformat(fact.period_start),
        end=date.fromisoformat(fact.period_end),
        vat_policy=vat_version.fact.policy,
        surtax_policy=surtax_version.fact.policy,
    )
    lines = []
    if assessment.relief_fen:
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
        if amount:
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
            "period_start": str(fact.period_start),
            "period_end": str(fact.period_end),
        },
        tuple(BalanceEffect(o["key"], o["amount_fen"], o["category"]) for o in obligations),
        ({"vat_policy_id": vat_version.id, "surtax_policy_id": surtax_version.id},),
    )


class IncomeTaxAssessment(Fact):
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


def calculate_income_tax_assessment(version: FactVersion, ctx: Context) -> Outcome:
    fact: IncomeTaxAssessment = version.fact
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
    difference = sum_fen((fact.cumulative_assessed_fen, -opening))
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
            "cumulative_assessed_fen": fact.cumulative_assessed_fen,
            "assessment_basis": fact.assessment_basis,
            "change_fen": difference,
            "obligations": [obligation],
        },
        (BalanceEffect(obligation["key"], abs(difference), obligation["category"]),),
    )


class TaxCreditReturn(TaxInput):
    """A credit-note source, explicitly attributed to an originally filed sale."""

    return_id: TaxSourceId
    original_sale_id: TaxSourceId
    return_kind: ClassVar[str] = "sale_return"
    original_kind: ClassVar[str] = "service_sale"

    @property
    def original_id(self):
        return self.original_sale_id


class TaxCreditAdvanceReturn(TaxInput):
    """An explicitly selected refund of an originally taxed customer advance."""

    return_id: TaxSourceId
    original_advance_id: TaxSourceId
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
    original_assessment_id: TaxSourceId
    original_period_start: ActualDate
    original_period_end: ActualDate
    returns: tuple[TaxCreditReturn | TaxCreditAdvanceReturn, ...] = Field(min_length=1)
    authority_confirmed: StrictBool | None = None
    original_filing_reference: str | None = Field(default=None, min_length=1)
    confirmation_reference: str | None = Field(default=None, min_length=1)
    original_filing_evidence: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmation_evidence: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
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
        if original.values["relief_fen"] and sale.values["exemption_eligible"]:
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
    registry.register(TaxAssessment, calculate_tax_assessment)
    registry.register(IncomeTaxAssessment, calculate_income_tax_assessment)
    registry.register(TaxCreditConfirmation, calculate_tax_credit_confirmation)
