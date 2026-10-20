"""Opening identity roles, complete current membership and explicit dependencies."""

from ai_accounting_modular.contracts.public import validate_contract

from ._common import PARTIES, fail, require_object

_ROLES = {
    "bank": (("funds_account_id", {"bank_account", "platform_account"}),),
    "cash": (("funds_account_id", {"cash_account"}),),
    "obligation": (("counterparty_id", PARTIES),),
    "asset": (("asset_id", {"asset"}),),
    "loan": (("lender_id", {"organization"}),),
    "tax": (("authority_id", {"organization"}),),
    "payroll_payable": (("person_id", {"person"}), ("recipient_id", PARTIES)),
    "equity": (("holder_or_basis_id", PARTIES),),
    "money_fund": (("fund_id", {"investment_product"}), ("counterparty_id", PARTIES)),
}


def validate_fact(fact, context=None):
    validate_contract("business-facts.schema.json", "Fact", fact)
    if fact["kind"] != "opening.package":
        fail("期初模块只接收opening.package。", "unsupported_business")
    if context is not None and fact["period"] != context.read_context.start_period:
        fail("期初包须属于本公司明确的建账月，不能在其他月份再次开账。", "invalid_input")
    if "effective_on" in fact and fact["effective_on"][:7] != fact["period"]:
        fail("期初资料的实际日必须属于明确建账月。", "invalid_input")
    members = fact["data"]["members"]
    ids = [item["member_id"] for item in members]
    if len(ids) != len(set(ids)):
        fail("期初成员的稳定身份不能重复。", "invalid_input")
    keys = []
    for member in members:
        data, kind = member["data"], member["data"]["type"]
        if kind in {"bank", "cash"}:
            key = ("funds", data["funds_account_id"])
        elif kind == "asset":
            key = (kind, data["asset_id"])
        elif kind == "loan":
            key = (kind, data["contract_id"])
        elif kind == "tax":
            key = (
                kind,
                data["authority_id"],
                data["tax_kind"],
                data["tax_period"],
                data["balance_kind"],
            )
        elif kind == "payroll_payable":
            key = (
                kind,
                data["person_id"],
                data["recipient_id"],
                data["payroll_period"],
                data["component"],
                data.get("tax_period"),
            )
        elif kind == "equity":
            key = (kind, data["equity_kind"], data["holder_or_basis_id"])
        elif kind == "money_fund":
            key = (kind, data["fund_id"], data["original_lot_reference"])
        else:
            key = (
                kind,
                data["counterparty_id"],
                data["nature"],
                data["original_business_reference"],
            )
        keys.append(key)
    if len(keys) != len(set(keys)):
        fail("同一真实期初余额、卡片、贷款或批次不能通过多个成员重复登记。", "invalid_input")


def roles(fact):
    for index, member in enumerate(fact["data"]["members"]):
        data = member["data"]
        for field, kinds in _ROLES[data["type"]]:
            yield index, field, data[field], kinds


def validate_roles(fact, context):
    for index, field, object_id, kinds in roles(fact):
        obj = require_object(fact, context, object_id, kinds, f"/data/members/{index}/data/{field}")
        data = fact["data"]["members"][index]["data"]
        if field == "fund_id" and obj["data"]["category"] != "money_fund":
            fail("期初货币基金成员不能套用其他投资产品。", "unsupported_business")
        if field == "asset_id" and obj["data"]["asset_type"] != data["asset_type"]:
            fail("期初卡片资产类别与稳定资产对象不一致。", "invalid_input")


def edge(business_id, producer_id, *, kind="object", mode="fact", relation="identity"):
    return {
        "consumer_business_id": business_id,
        "consumer_result_slot": "main",
        "producer_kind": kind,
        "producer_id": producer_id,
        "producer_public_slot": "main",
        "read_mode": mode,
        "relation_kind": relation,
    }


def dependencies(fact):
    result = {
        ("object", object_id): edge(fact["business_id"], object_id)
        for _, _, object_id, _ in roles(fact)
    }
    for member in fact["data"]["members"]:
        data = member["data"]
        if data["type"] == "loan":
            result[("business", data["contract_id"])] = edge(
                fact["business_id"],
                data["contract_id"],
                kind="business",
                mode="result",
                relation="calculation",
            )
    return list(result.values())


def member_object(member):
    data = member["data"]
    return next(
        (
            data[field]
            for field in (
                "funds_account_id",
                "counterparty_id",
                "asset_id",
                "lender_id",
                "authority_id",
                "person_id",
                "holder_or_basis_id",
                "fund_id",
            )
            if field in data
        ),
        None,
    )
