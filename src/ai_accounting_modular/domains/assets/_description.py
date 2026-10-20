"""Declare complete monthly responsibilities and exact asset dependency edges."""

from ai_accounting_modular.contracts.public import canonical_json

from ._cards import activated_cards, consumption_end, related
from ._common import add_months, assets_in, month_at, month_number
from ._consumption import slot_for
from ._facts import roles, validate_fact
from ._inputs import AssetInputs
from ._origins import opening_geometry


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
    result = {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": slot,
        "producer_kind": producer_kind,
        "producer_id": producer_id,
        "producer_public_slot": producer_slot,
        "read_mode": mode,
        "relation_kind": relation,
        **scope,
    }
    if mode == "use":
        result["resource_components"] = ["cost"]
    return result


def activation_months(fact, context):
    months = set()
    for _, card in activated_cards(fact, context):
        end = min(consumption_end(card, context), context.evaluation_period_to)
        for number in range(month_number(card["consumption_start"]), month_number(end) + 1):
            months.add(month_at(number))
    return sorted(months)


def publications(fact, slots):
    result = []
    for slot in slots:
        month = slot.removeprefix("depreciation:") if slot != "main" else fact["period"]
        if slot != "main" or fact["kind"] in {"assets.project_release", "assets.disposal"}:
            result.append(
                {
                    "business_id": fact["business_id"],
                    "result_slot": slot,
                    "collection": "pre_tax",
                    "period_from": month,
                    "period_to": month,
                }
            )
        if fact["kind"] == "assets.disposal" and fact["data"]["type"] == "sale":
            result.append(
                {
                    "business_id": fact["business_id"],
                    "result_slot": slot,
                    "collection": "tax_sales",
                    "period_from": month,
                    "period_to": month,
                }
            )
    return result


def source_scopes(fact, context):
    if not assets_in(fact):
        return []
    start = context.read_context.start_period
    return [
        _edge(
            fact,
            "scope",
            "opening.package",
            mode="collection",
            relation="cost",
            period_from=start,
            period_to=start,
        ),
        _edge(
            fact,
            "scope",
            "assets.disposal",
            mode="collection",
            relation="calculation",
            period_from=start,
            period_to=context.evaluation_period_to,
        ),
    ]


def monthly_dependencies(fact, slots):
    edges, declared = [], set(slots)
    for slot in slots:
        if slot == "main":
            continue
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
        if prior in declared:
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
    return edges


def direct_dependencies(fact):
    edges = [
        _edge(fact, "object", object_id)
        for object_id in dict.fromkeys(role[1] for role in roles(fact))
    ]
    edges.extend(
        _edge(fact, "business", item["source"]["business_id"], mode="result", relation="cost")
        for item in fact["data"].get("sources", ())
    )
    if fact["kind"] == "assets.disposal" and fact["data"]["type"] == "sale":
        edges.append(
            _edge(
                fact,
                "business",
                fact["data"]["used_asset_vat_policy_id"],
                mode="result",
                relation="policy",
            )
        )
    return edges


def dependencies(fact, slots, context):
    edges = direct_dependencies(fact)
    edges.extend(source_scopes(fact, context))
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
                    edges.append(
                        _edge(
                            fact,
                            "business",
                            acquired["business_id"],
                            producer_slot="cost:" + asset_id,
                            mode="use",
                            relation="cost",
                            period_from=fact["period"],
                            period_to=fact["period"],
                        )
                    )
            if kind == "assets.activation":
                for disposed in related(context, asset_id, "assets.disposal"):
                    edges.append(
                        _edge(fact, "business", disposed["business_id"], relation="calculation")
                    )
            elif kind == "assets.disposal":
                for opened in related(context, asset_id, "opening.package"):
                    edges.append(
                        _edge(
                            fact,
                            "business",
                            opened["business_id"],
                            mode="result",
                            relation="calculation",
                        )
                    )
                    geometry = next(
                        item for item in opening_geometry(opened) if item["asset_id"] == asset_id
                    )
                    end = min(consumption_end(geometry, context), fact["period"])
                    if end >= max(context.read_context.start_period, geometry["consumption_start"]):
                        edges.append(
                            _edge(
                                fact,
                                "business",
                                opened["business_id"],
                                producer_slot=slot_for(end),
                                mode="result",
                                relation="calculation",
                            )
                        )
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
            _edge(
                fact,
                "business",
                item["source"]["business_id"],
                producer_slot=item["source"]["slot"],
                mode="use",
                relation="cost",
                period_from=fact["period"],
                period_to=fact["period"],
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
        edges.extend(monthly_dependencies(fact, slots))
    unique = {canonical_json(edge): edge for edge in edges}
    return list(unique.values())
