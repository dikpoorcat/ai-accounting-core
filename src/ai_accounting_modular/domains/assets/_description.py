"""Declare complete monthly responsibilities and exact asset dependency edges."""

from ._cards import activated_cards, consumption_end, related
from ._common import add_months, assets_in, month_at, month_number
from ._consumption import slot_for
from ._facts import roles, validate_fact
from ._inputs import AssetInputs


class DescriptionContext(AssetInputs):
    """Only overlay this owner's explicit changes, never search foreign JSON."""

    def __init__(self, context, changes):
        facts, shadowed = [], set()
        for change in changes:
            business_id = change.get("business_id", change.get("fact", {}).get("business_id"))
            shadowed.add(business_id)
            if change["op"] not in {"withdraw", "handoff_out"}:
                fact = change.get("fact") or context.get_fact(business_id)
                if fact is not None:
                    validate_fact(fact)
                    facts.append(fact)
        super().__init__(context, facts=facts, shadowed=shadowed)
        self.load_assets(item["asset_id"] for fact in facts for item in assets_in(fact))


def _edge(
    fact,
    producer_kind,
    producer_id,
    *,
    slot="main",
    producer_slot="main",
    mode="fact",
    relation="identity",
    **scope,
):
    return {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": slot,
        "producer_kind": producer_kind,
        "producer_id": producer_id,
        "producer_public_slot": producer_slot,
        "read_mode": mode,
        "relation_kind": relation,
        **scope,
    }


def activation_months(fact, context):
    months = set()
    for _, card in activated_cards(fact, context):
        end = min(consumption_end(card, context), context.evaluation_period_to)
        for number in range(month_number(card["consumption_start"]), month_number(end) + 1):
            months.add(month_at(number))
    return sorted(months)


def dependencies(fact, slots, context):
    edges = [
        _edge(fact, "object", object_id)
        for object_id in dict.fromkeys(role[1] for role in roles(fact))
    ]
    data, kind = fact["data"], fact["kind"]
    if kind in {"assets.acquisition", "assets.activation", "assets.disposal"}:
        for card in assets_in(fact):
            asset_id = card["asset_id"]
            edges.append(_edge(fact, "object", asset_id, mode="collection"))
            if kind in {"assets.activation", "assets.disposal"}:
                for acquired in related(context, asset_id, "assets.acquisition"):
                    edges.append(
                        _edge(
                            fact,
                            "business",
                            acquired["business_id"],
                            mode="result",
                            relation="cost",
                        )
                    )
            if kind == "assets.activation":
                for disposed in related(context, asset_id, "assets.disposal"):
                    edges.append(
                        _edge(fact, "business", disposed["business_id"], relation="calculation")
                    )
            elif kind == "assets.disposal":
                for enabled in related(context, asset_id, "assets.activation"):
                    edges.append(
                        _edge(
                            fact,
                            "business",
                            enabled["business_id"],
                            mode="result",
                            relation="calculation",
                        )
                    )
                    for _, enabled_card in activated_cards(enabled, context):
                        if enabled_card["asset_id"] == asset_id:
                            end = consumption_end(enabled_card, context)
                            if end >= enabled_card["consumption_start"]:
                                edges.append(
                                    _edge(
                                        fact,
                                        "business",
                                        enabled["business_id"],
                                        producer_slot=slot_for(end),
                                        mode="result",
                                        relation="calculation",
                                    )
                                )
    for item in data.get("sources", ()):
        edges.append(
            _edge(fact, "business", item["source"]["business_id"], mode="result", relation="cost")
        )
    if kind == "assets.disposal" and data["type"] == "sale":
        edges.append(
            _edge(
                fact, "business", data["used_asset_vat_policy_id"], mode="result", relation="policy"
            )
        )
    if kind == "assets.activation":
        cards = activated_cards(fact, context)
        terminal = max(consumption_end(card, context) for _, card in cards)
        edges.append(
            _edge(
                fact,
                "calendar",
                "work",
                mode="collection",
                relation="calculation",
                period_from=fact["period"],
                period_to=terminal,
            )
        )
        for slot in slots[1:]:
            edges.append(
                _edge(
                    fact,
                    "business",
                    fact["business_id"],
                    slot=slot,
                    mode="result",
                    relation="calculation",
                )
            )
            prior = slot_for(add_months(slot[len("depreciation:") :], -1))
            if prior in slots:
                edges.append(
                    _edge(
                        fact,
                        "business",
                        fact["business_id"],
                        slot=slot,
                        producer_slot=prior,
                        mode="result",
                        relation="calculation",
                    )
                )
    unique = {tuple(sorted(edge.items())): edge for edge in edges}
    return list(unique.values())
