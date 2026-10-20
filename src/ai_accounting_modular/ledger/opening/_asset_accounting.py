"""Opening retains ownership while an injected public asset port computes its slots."""

from ai_accounting_modular.contracts.public import OpeningAssetSlots, validate_contract

from ._common import fail

_FIELDS = {"complete_result_slots", "dependencies", "publications", "issues"}


def has_assets(fact):
    return any(member["data"]["type"] == "asset" for member in fact["data"]["members"])


class AssetAccounting:
    def __init__(self, port):
        self._port = port

    def require_for(self, fact):
        present = has_assets(fact)
        if present and self._port is None:
            fail(
                "含资产卡片的期初须装配资产核算端口，才能完整接续后续摊折。", "unsupported_business"
            )
        return present

    def describe(self, fact, context) -> OpeningAssetSlots:
        if not self.require_for(fact):
            return {
                "complete_result_slots": [],
                "dependencies": [],
                "publications": [],
                "issues": [],
            }
        result = self._port.describe_opening_assets(fact, context)
        if (
            type(result) is not dict
            or set(result) != _FIELDS
            or any(type(result[field]) is not list for field in _FIELDS)
        ):
            fail("资产协作描述须完整提供约定的四项列表。", "invalid_input")
        if "main" in result["complete_result_slots"]:
            fail("资产协作不能接管期初的main结果槽。", "invalid_input")
        for slot in result["complete_result_slots"]:
            validate_contract("core.schema.json", "Slot", slot)
        allowed = {"main", *result["complete_result_slots"]}
        for edge in result["dependencies"]:
            validate_contract("business-facts.schema.json", "Dependency", edge)
            if (
                edge["consumer_business_id"] != fact["business_id"]
                or edge["consumer_result_slot"] not in allowed
            ):
                fail("资产协作依赖须属于当前期初的完整结果槽。", "invalid_input")
        for publication in result["publications"]:
            validate_contract("business-facts.schema.json", "ResultPublication", publication)
            if (
                publication["business_id"] != fact["business_id"]
                or publication["result_slot"] not in allowed
            ):
                fail("资产协作发布须属于当前期初的完整结果槽。", "invalid_input")
        return result

    def evaluate(self, fact, result_slot, context):
        if not self.require_for(fact):
            fail("没有资产卡片的期初不承担附加资产结果槽。", "invalid_input")
        plan = self._port.evaluate_opening_asset_slot(fact, result_slot, context)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        if (plan["business_id"], plan["result_slot"]) != (fact["business_id"], result_slot):
            fail("资产协作返回了其他业务或其他结果槽的计划。", "invalid_input")
        return plan

    def cards(self, fact, context):
        if not self.require_for(fact):
            return {}
        values = self._port.opening_asset_cards(fact, context)
        if type(values) is not tuple:
            fail("资产协作须返回约定的完整期初卡片元组。", "invalid_input")
        expected = {
            member["data"]["asset_id"]: member["data"]
            for member in fact["data"]["members"]
            if member["data"]["type"] == "asset"
        }
        result = {}
        for card in values:
            validate_contract("business-facts.schema.json", "AdoptedAssetQuantity", card)
            asset_id = card["asset_id"]
            if asset_id not in expected or asset_id in result:
                fail("资产协作卡片包含其他资产或重复资产身份。", "invalid_input")
            if any(
                card[field] != expected[asset_id][field]
                for field in ("asset_type", "cost_fen", "accumulated_fen")
            ):
                fail("资产协作卡片不能改写期初明确的类别、原价或累计额。", "invalid_input")
            result[asset_id] = card
        if result.keys() != expected.keys():
            fail("资产协作未提供本单全部期初卡片。", "invalid_input")
        return result

    def validate(self, plan, context):
        if self._port is not None:
            self._port.validate_opening_asset_candidate(plan, context)
