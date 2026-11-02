"""2011 No.36 interest belongs to the actual holding interval, not a whole coupon."""

from copy import deepcopy
from datetime import date
from fractions import Fraction

from ._common import amount, missing_investment_input, reject, total
from ._investment_units import rounded_fraction

OFFICIAL_URL = "https://shanghai.chinatax.gov.cn/hktax/tzgg/tzl/201107/t344673.html"


def _missing(view, field, message, entitlement=None, *, terms=False):
    event = view.event
    source = (entitlement or {}).get("origin_source", (entitlement or {}).get("source"))
    identity = (
        event["terms_id"]
        if terms
        else source["business_id"]
        if source
        else view.source["business_id"]
    )
    if source is not None and source["slot"].startswith("member:") and not terms:
        message = f"期初投资成员{source['slot']}：{message}"
        field = "/data/members"
    missing_investment_input(
        event,
        identity,
        field,
        message,
        precision=("day", "annual_rate"),
        meaning="国债税务持有期间利息的现实依据",
    )


def _rate_interest(view, start, end, nominal, terms):
    periods = sorted(terms["rate_periods"], key=lambda row: (row["start_on"], row["end_before"]))
    for previous, current in zip(periods, periods[1:], strict=False):
        if previous["end_before"] > current["start_on"]:
            reject("国债适用年率的有源区间不能重叠。", "invalid_input")
    base = Fraction(nominal)
    if terms["amount_basis"] == "issue_price":
        if "issue_price_per_100_face_fen" not in terms:
            _missing(
                view,
                "/data/tax_facts/government_bond_eit/issue_price_per_100_face_fen",
                "以发行价格为计息金额需要券资料中的每百元面值发行价。",
                terms=True,
            )
        base *= Fraction(amount(terms["issue_price_per_100_face_fen"]), 10000)
    cursor, result, sections = start, Fraction(), []
    for period in periods:
        if period["start_on"] >= period["end_before"]:
            reject("国债适用年率须为正向真实日期区间。", "invalid_input")
        left, right = max(start, period["start_on"]), min(end, period["end_before"])
        if left >= right:
            continue
        if left != cursor:
            _missing(
                view,
                "/data/tax_facts/government_bond_eit/rate_periods",
                "所采用年率没有完整覆盖本次实际持有区间。",
                terms=True,
            )
        days = (date.fromisoformat(right) - date.fromisoformat(left)).days
        result += base * Fraction(period["annual_rate"]) * days / 365
        sections.append(
            {
                "start_on": left,
                "end_before": right,
                "days": days,
                "annual_rate": period["annual_rate"],
            }
        )
        cursor = right
    if cursor != end:
        _missing(
            view,
            "/data/tax_facts/government_bond_eit/rate_periods",
            "本次国债持有期间缺少有源适用年率，不按息票比率或建账月份推算。",
            terms=True,
        )
    return result, sections


def realization(view):
    event, kind = view.event, view.event["event_kind"]
    if event["tax_facts"]["instrument_type"] != "government_bond":
        return None
    if kind == "disposal" and event.get("exit_mode") == "maturity":
        return None  # Direct-bond coupon is a separate, already sourced realization.
    if kind not in {"income", "debt_interest", "disposal"}:
        return None
    if kind in {"income", "debt_interest"} and event.get("income_character") != "interest":
        return None
    entitlements = event.get("bond_entitlements")
    if not entitlements:
        _missing(
            view, "/data/source", "国债免税利息需要实际采用批次的原始取得方式、持有期间及票面金额。"
        )
    when = view.effective_on
    if when is None:
        _missing(
            view, "/effective_on", "国债税息实现需要真实合同付息日或实际转让日，不能用月末默认。"
        )
    coupon_dates = event.get("bond_coupon_dates")
    if not coupon_dates:
        _missing(
            view,
            "/data/coupon_schedule",
            "国债免税利息需要所采用券合同的完整付息日期。",
            terms=True,
        )
    coupon_dates = sorted(set(coupon_dates))
    final_coupon = event["tax_facts"].get("maturity_on", coupon_dates[-1])
    full_coupon = kind != "disposal" and when == final_coupon
    face_total = total(amount(row["nominal_fen"]) for row in entitlements)
    if face_total <= 0:
        reject("国债利息来源的实际票面金额必须为正。", "invalid_input")
    details, raw_total, allocated = [], Fraction(), 0
    for entitlement in sorted(
        entitlements, key=lambda row: (row["source"]["business_id"], row["source"]["slot"])
    ):
        carry = entitlement.get("eit_interest_realized_through")
        if carry is not None:
            if carry > when:
                reject("已实现税息接续边界不能晚于本次实现日。", "invalid_input")
            if "eit_interest_source" not in entitlement:
                _missing(
                    view,
                    "/data/eit_interest_source",
                    "国债已实现税息接续需要原始资料证明。",
                    entitlement,
                )
        origin = entitlement.get("acquisition_origin")
        if origin is None and full_coupon and carry != when:
            _missing(
                view,
                "/data/acquisition_origin",
                "需核实该批国债为一级发行认购或二级取得，不能选择全额免税。",
                entitlement,
            )
        nominal = amount(entitlement["nominal_fen"])
        row = {"source": deepcopy(entitlement["source"]), "nominal_fen": str(nominal)}
        if carry == when:
            raw = Fraction()
            row.update(method="holding_days", start_on=when, end_before=when, rate_sections=[])
        elif origin == "primary_subscription" and full_coupon and amount(event["gross_price_fen"]):
            raw = Fraction(amount(event["gross_price_fen"]) * nominal, face_total)
            row.update(method="primary_to_maturity", rate_sections=[])
        else:
            previous = [
                day for day in coupon_dates if day < when or kind == "disposal" and day == when
            ]
            boundary = max(previous, default=None)
            if carry is not None:
                boundary = max(boundary or carry, carry)
            acquired = entitlement.get("acquired_on")
            if acquired is None:
                acquired_period = entitlement.get("acquired_period")
                if (
                    boundary is not None
                    and acquired_period is not None
                    and acquired_period < boundary[:7]
                ):
                    acquired = (
                        boundary  # Exact old day cannot affect max(old acquisition, known coupon).
                    )
                else:
                    _missing(
                        view,
                        "/effective_on",
                        "该批国债缺少影响实际持有天数的原始取得日，不能默认建账日。",
                        entitlement,
                    )
            start = max(acquired, boundary or acquired)
            if start > when:
                reject("国债真实持有起日不能晚于本次税息实现日。", "invalid_input")
            if start == when:
                raw, sections = Fraction(), []
            else:
                terms = event["tax_facts"].get("government_bond_eit")
                if terms is None:
                    _missing(
                        view,
                        "/data/tax_facts/government_bond_eit",
                        "需采用券资料的适用年率或折合年收益率，不能将每期coupon_rate当年率。",
                        terms=True,
                    )
                raw, sections = _rate_interest(view, start, when, nominal, terms)
            row.update(
                method="holding_days", start_on=start, end_before=when, rate_sections=sections
            )
        raw_total += raw
        through = rounded_fraction(raw_total)
        row["gross_exempt_interest_fen"] = str(through - allocated)
        allocated = through
        details.append(row)
    quantity = {
        "type": "government_bond_interest_realization",
        "source": deepcopy(view.source),
        "event_index": view.event_index,
        "realized_on": when,
        "gross_interest_numerator": str(raw_total.numerator),
        "gross_interest_denominator": str(raw_total.denominator),
        "gross_exempt_interest_fen": str(allocated),
        "entitlements": details,
    }
    return allocated, quantity
