"""A real asset has one sourced cost origin, from acquisition or opening."""

from copy import deepcopy

from ._common import add_months, assets_in, missing, reject, result_for


def opening_geometry(fact):
    """Describe months from typed facts without evaluating opening balances."""
    return tuple(
        {
            "asset_id": item["asset_id"],
            "consumption_start": add_months(
                item["in_use_period"], int(item["asset_type"] == "fixed")
            ),
            "useful_life_months": item["useful_life_months"],
        }
        for item in assets_in(fact)
    )


def origin_facts(context, asset_id):
    return (
        *context.assets_related(asset_id, "assets.acquisition"),
        *context.assets_related(asset_id, "opening.package"),
    )


def unique_origin(fact, context, asset_id):
    origins = origin_facts(context, asset_id)
    if len(origins) > 1:
        reject("同一资产只能有一份期初成本或正常取得成本，不能重复承接。")
    if not origins:
        missing(fact, "/data/asset_id", "资产缺少公司取得或期初卡片的真实依据。")
    return origins[0]


def opening_card(fact, context, asset_id):
    result = result_for(context, fact["business_id"])
    if result.target["kind"] != "opening.package":
        reject("期初资产须采用该期初业务main的公开卡片依据。")
    return result, deepcopy(context.adopted_asset_card(result, asset_id))


def validate_origins(context, asset_ids):
    for asset_id in set(asset_ids):
        origins = origin_facts(context, asset_id)
        activations = context.assets_related(asset_id, "assets.activation")
        if (
            len(origins) > 1
            or len(activations) > 1
            or len(context.assets_related(asset_id, "assets.disposal")) > 1
        ):
            reject("同一资产不能重复取得、承接期初、启用或处置。")
        if any(fact["kind"] == "opening.package" for fact in origins) and activations:
            reject("期初在用卡片直接接续，不能再登记一次启用。")
