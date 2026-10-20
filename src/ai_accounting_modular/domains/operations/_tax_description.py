"""Declare monthly tax responsibilities before any candidate amounts are read."""

from ._tax_lifecycle import future_months, result_slots


def slots(fact, context):
    if fact["kind"] == "operations.service_sale" and fact["data"]["gross_fen"] == "0":
        return ["main"]
    if fact["kind"] == "operations.advance_fulfillment":
        original = context.get_fact(fact["data"]["advance_id"])
        point = fact["data"].get("tax_point")
        old_point = (original or {}).get("data", {}).get("tax_point")
        same = (
            point is not None
            and old_point is not None
            and (
                {key: value for key, value in point.items() if key != "source"}
                == {key: value for key, value in old_point.items() if key != "source"}
            )
        )
        if point is None or same:
            return ["main"]
    if fact["kind"] in {"operations.service_sale", "operations.advance_fulfillment"} or (
        fact["kind"] == "operations.advance" and fact["data"]["side"] == "customer"
    ):
        return result_slots(fact, context)
    return ["main"]


def _edge(fact, producer, *, consumer_slot="main", producer_slot="main", mode="result", **scope):
    return {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": consumer_slot,
        "producer_kind": "business",
        "producer_id": producer,
        "producer_public_slot": producer_slot,
        "read_mode": mode,
        "relation_kind": "calculation",
        **scope,
    }


def dependencies(fact, context, result_slots):
    found = []
    point = fact["data"].get("tax_point")
    if point and point["type"] == "derived":
        found.append(_edge(fact, point["tax_point_id"]))
    is_source = fact["kind"] == "operations.service_sale" or (
        fact["kind"] == "operations.advance" and fact["data"]["side"] == "customer"
    )
    if is_source:
        found.append(
            _edge(
                fact,
                fact["business_id"],
                mode="collection",
                period_from="0001-01",
                period_to="9999-12",
            )
        )
        field = "sale_id" if fact["kind"] == "operations.service_sale" else "advance_id"
        found.extend(
            _edge(fact, member["business_id"], mode="fact")
            for member in context.related_facts(fact["business_id"], "calculation")
            if member["kind"] in {"operations.sale_return", "operations.advance_refund"}
            and member["data"][field] == fact["business_id"]
            and member["period"] == fact["period"]
        )
    months = future_months(fact, context)
    if months:
        found.append(
            {
                **_edge(
                    fact,
                    "work",
                    mode="collection",
                    period_from=fact["period"],
                    period_to=max(months),
                ),
                "producer_kind": "calendar",
            }
        )
    for slot in result_slots[1:]:
        found.append(_edge(fact, fact["business_id"], consumer_slot=slot))
        found.append(
            _edge(
                fact,
                fact["business_id"],
                consumer_slot=slot,
                mode="collection",
                period_from="0001-01",
                period_to="9999-12",
            )
        )
        source_field = "advance_id" if fact["kind"] == "operations.advance" else "sale_id"
        for member in context.related_facts(fact["business_id"], "calculation"):
            if member["data"].get(source_field) == fact["business_id"] and (
                member["period"] < slot[4:]
                or (
                    member["period"] == slot[4:]
                    and member["kind"] in {"operations.sale_return", "operations.advance_refund"}
                )
            ):
                found.append(_edge(fact, member["business_id"], consumer_slot=slot))
    field = "sale_id" if fact["kind"] == "operations.sale_return" else "advance_id"
    original = context.get_fact(fact["data"][field]) if field in fact["data"] else None
    if original is not None:
        if fact["kind"] == "operations.advance_fulfillment":
            found.extend(
                _edge(fact, member["business_id"], mode="fact")
                for member in context.related_facts(original["business_id"], "calculation")
                if member["kind"] == "operations.advance_refund"
                and member["data"]["advance_id"] == original["business_id"]
                and member["period"] <= fact["period"]
            )
        for month in future_months(original, context):
            if month < fact["period"] or (
                month == fact["period"] and fact["kind"] == "operations.advance_fulfillment"
            ):
                found.append(_edge(fact, original["business_id"], producer_slot="tax:" + month))
    return found
