"""Purchased due coupons are receivables, never a new holding-period return."""

from decimal import Decimal, localcontext

from ._common import fail, missing, total
from ._investment_math import rounded


def acquired_coupons(fact, terms, units, declared):
    data = fact["data"]
    supplied = data.get("acquired_coupon_dates")
    if terms["right_type"] != "direct_bond":
        if supplied is not None:
            fail("取得前已到息合同日期只用于直接债券，不能套用于其他投资。", "invalid_input")
        return None
    schedule = {row["payment_on"]: row for row in terms["coupon_schedule"]}
    acquired_on = fact.get("effective_on")
    if acquired_on is None and any(day[:7] == fact["period"] for day in schedule):
        missing(
            fact,
            "/effective_on",
            "本月有合同付息日，真实取得日会改变旧息权利和后续摊销区间，须先核对交易确认单。",
            precision=("day",),
        )
    if declared and not supplied:
        missing(
            fact,
            "/data/acquired_coupon_dates",
            "价款已单列取得前应收利息，须从交易确认单对应到具体已到息合同日期。",
            precision=("source", "day"),
        )
    if acquired_on in schedule and supplied is None:
        missing(
            fact,
            "/data/acquired_coupon_dates",
            "取得当日恰为合同付息日，须明确随本次取得的旧息权利；有据不包含时明确给空列表，不能把未知当零。",
            precision=("source", "day"),
        )
    if supplied is None:
        return None
    if len(supplied) != len(set(supplied)):
        fail("同一已到息权利不能重复列入取得价款。", "invalid_input")
    due = []
    for day in supplied:
        if (
            day not in schedule
            or (acquired_on is not None and day > acquired_on)
            or (acquired_on is None and day[:7] >= fact["period"])
        ):
            fail("取得前应收利息只能绑定合同已有且在真实取得日已经到息的权利。", "invalid_input")
        with localcontext() as arithmetic:
            arithmetic.prec = 80
            due.append(rounded(units * Decimal(schedule[day]["coupon_rate"])))
    if total(due) != declared:
        fail("取得前应收利息须等于所列合同日期按本批面值计算的已到息权利合计。", "invalid_input")
    return tuple(sorted(supplied))


def holding_coupon_dates(lot, terms, *, start_period=None):
    """Only uncompleted contract periods belong to this holding's accounting."""
    beginning = max(lot.acquired_period, start_period or lot.acquired_period)
    return tuple(
        row["payment_on"]
        for row in terms.get("coupon_schedule", ())
        if beginning <= row["payment_on"][:7]
        and row["payment_on"] not in (lot.acquired_coupon_dates or ())
        and (lot.acquired_on is None or row["payment_on"] > lot.acquired_on)
    )
