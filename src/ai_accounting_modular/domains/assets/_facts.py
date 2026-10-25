"""Strict asset facts, real object roles, and intrinsic uniqueness."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._acquisition import acquisition, check_acquisition
from ._cards import activation
from ._common import assets_in, issue, reject, unique_items
from ._disposal import disposal
from ._projects import project_cost, project_release

CALCULATORS = {
    "assets.project_cost": project_cost,
    "assets.project_release": project_release,
    "assets.acquisition": acquisition,
    "assets.activation": activation,
    "assets.disposal": disposal,
}


def roles(fact):
    data, roles = fact["data"], []
    # Issue pointers follow the submitted list, even though formal card sets sort by ID.
    items = fact["data"].get("items", fact["data"].get("assets", assets_in(fact)))
    for index, item in enumerate(items):
        if fact["kind"] == "assets.activation":
            field = f"/data/items/{index}/asset_id"
        elif fact["kind"] == "assets.disposal":
            field = "/data/asset_id"
        elif data["type"] == "reimbursed_batch":
            field = f"/data/assets/{index}/asset_id"
        else:
            field = "/data/asset/asset_id"
        roles.append((field, item["asset_id"], {"asset"}))
    if "project_id" in data:
        roles.append(("/data/project_id", data["project_id"], {"project"}))
    if "supplier_id" in data:
        roles.append(("/data/supplier_id", data["supplier_id"], {"person", "organization"}))
    if "buyer_id" in data:
        roles.append(("/data/buyer_id", data["buyer_id"], {"person", "organization"}))
    for index, creditor in enumerate(data.get("creditors", ())):
        roles.append((f"/data/creditors/{index}/person_id", creditor["person_id"], {"person"}))
    return roles


def check_roles(fact, context):
    issues = []
    cards = {item["asset_id"]: item for item in assets_in(fact)}
    for field, object_id, kinds in roles(fact):
        obj = context.get_object(object_id)
        if obj is None:
            problem = issue(fact, field, "引用的资产、项目、人员或交易方尚未在本公司登记。")
            problem["code"] = "reference_not_found"
            issues.append(problem)
        elif obj["kind"] not in kinds:
            problem = issue(
                fact, field, "引用对象不符合本项资产业务角色，账户不能冒充资产或交易方。"
            )
            problem["code"] = "incompatible_object_kind"
            issues.append(problem)
        elif obj["kind"] == "asset":
            card = cards.get(object_id)
            if (
                card
                and "asset_type" in card
                and obj.get("data", {}).get("asset_type") != card["asset_type"]
            ):
                problem = issue(fact, field, "资产事实类型须与稳定资产档案一致。")
                problem["code"] = "incompatible_object_kind"
                issues.append(problem)
    if issues:
        raise ContractError(
            "invalid_input", "资产所填对象身份不符合其业务角色。", fact_issues=issues
        )


def validate_fact(fact):
    if fact["kind"] == "assets.disposal" and "effective_on" not in fact:
        raise ContractError(
            "needs_information",
            "资产处置需要真实处置日。",
            fact_issues=[
                issue(fact, "/effective_on", "请提供实际处置日。", precision=("day",)),
            ],
        )
    validate_contract("business-facts.schema.json", "Fact", fact)
    if fact["kind"] not in CALCULATORS:
        reject("资产模块不承接其他类型事实。", "unsupported_business")
    if fact.get("effective_on") and fact["effective_on"][:7] != fact["period"]:
        reject("已知资产实际日期必须属于核算所属月份。", "invalid_input")
    if fact["kind"] == "assets.acquisition":
        check_acquisition(fact)
    elif fact["kind"] == "assets.activation":
        unique_items(fact["data"]["items"], "asset_id", "同一启用批次不能重复资产身份。")


def natural_key(fact):
    if fact["kind"] in {"assets.acquisition", "assets.activation", "assets.disposal"}:
        cards = assets_in(fact)
        if len(cards) == 1:
            return fact["kind"] + ":" + cards[0]["asset_id"]
    return None
