"""Keep old amount-only fund holdings visible without inventing tax inventory."""

from copy import deepcopy

from ._common import amount, issue, reject
from ._investment_pools import affects_disposal

KINDS = {"simple_fund_acquisition", "simple_fund_redemption"}


def separate(consumer, previous, views):
    positions, lots = [], {}
    for row in previous:
        if "simple_fund_lots" not in row:
            positions.append(row)
            continue
        for lot in row["simple_fund_lots"]:
            source = lot["source"]["business_id"]
            if source in lots:
                reject("同一旧货基成本来源不能重复出现在税务接续中。")
            lots[source] = row["product_id"], deepcopy(lot)
    ordinary, simple, acquisitions = [], [], {}
    for view in views:
        (simple if view.event["event_kind"] in KINDS else ordinary).append(view)
    for view in simple:
        event = view.event
        if event["event_kind"] != "simple_fund_acquisition":
            continue
        key = view.source["business_id"]
        if key in lots:
            reject("旧货基取得来源已经承接，不能重复增加。")
        lots[key] = (
            event["product_id"],
            {
                "source": deepcopy(view.source),
                "remaining_cost_fen": event["accounting_cost_fen"],
            },
        )
        acquisitions[key] = view
    for view in simple:
        event = view.event
        if event["event_kind"] != "simple_fund_redemption":
            continue
        unsupported(consumer, view.source["business_id"])
    exits = {}
    for view in ordinary:
        if view.event["event_kind"] == "disposal" and view.event.get("right_type") == "fund_units":
            exits.setdefault(view.event["product_id"], []).append(view)
    for product, lot in lots.values():
        acquisition = acquisitions.get(lot["source"]["business_id"])
        if amount(lot["remaining_cost_fen"]) and any(
            affects_disposal(acquisition, disposal, consumer["data"].get("method"))
            for disposal in exits.get(product, ())
        ):
            unsupported(consumer, lot["source"]["business_id"])
    grouped = {}
    for product, lot in lots.values():
        if amount(lot["remaining_cost_fen"]):
            grouped.setdefault(product, []).append(lot)
    carried = [
        {
            "product_id": product,
            "simple_fund_lots": sorted(rows, key=lambda row: row["source"]["business_id"]),
        }
        for product, rows in sorted(grouped.items())
    ]
    return positions, tuple(ordinary), carried


def unsupported(consumer, source_id):
    message = (
        f"当前投资税务计算涉及旧货基来源 {source_id}；该旧业务只保存金额成本，"
        "尚未提供份额及完整税库存接续能力，不能用账面成本代替VAT均价或要求补不存在的输入字段。"
    )
    reject(
        message,
        "unsupported_business",
        fact_issues=[
            issue(
                consumer,
                "/",
                message,
                code="unsupported_calculation",
                meaning="当前旧货基接口的税库存能力边界",
            )
        ],
    )
