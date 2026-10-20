"""One responsibility and explicit current dependencies for each complete fact."""

from ai_accounting_modular.contracts.public import validate_contract

from ._common import ACCOUNTS, fail, require_object
from ._cooperation_common import KINDS as COOPERATION_KINDS
from ._cooperation_common import PREFIX
from ._interest import interval

FINANCING_KINDS = (
    "financing.owner_funding",
    "financing.loan_contract",
    "financing.loan_drawdown",
    "financing.interest_period",
)
INVESTMENT_KINDS = (
    "investment.money_fund_purchase",
    "investment.money_fund_redemption",
    *COOPERATION_KINDS,
)


def validate_fact(fact, family):
    validate_contract("business-facts.schema.json", "Fact", fact)
    supported = FINANCING_KINDS if family == "financing" else INVESTMENT_KINDS
    if fact["kind"] not in supported:
        fail("融资投资模块尚未实现该种业务。", "unsupported_business")
    if "effective_on" in fact and fact["effective_on"][:7] != fact["period"]:
        fail("实际日必须属于核算月。", "invalid_input")
    if fact["kind"] == "financing.interest_period":
        interval(fact)
    if fact["kind"] == "investment.money_fund_redemption":
        refs = [(x["source"]["business_id"], x["source"]["slot"]) for x in fact["data"]["costs"]]
        if len(refs) != len(set(refs)):
            fail("赎回成本不能重复引用同一公开批次。", "invalid_input")
    if fact["kind"].startswith(PREFIX):
        data = fact["data"]
        if "actual_on" in data and (
            data["actual_on"][:7] != fact["period"]
            or "effective_on" in fact
            and fact["effective_on"] != data["actual_on"]
        ):
            fail("合作实际收付日须属于核算月，不能与已给的实际日矛盾。", "invalid_input")


def object_roles(fact):
    data, result = fact["data"], []
    for field in (
        "owner_id",
        "lender_id",
        "fund_id",
        "counterparty_id",
        "funds_account_id",
        "project_id",
        "operator_id",
    ):
        if field not in data:
            continue
        kinds = (
            {"organization"}
            if field == "lender_id"
            else {"investment_product"}
            if field in {"fund_id", "project_id"}
            else {"bank_account"}
            if field == "funds_account_id" and fact["kind"] == "financing.loan_drawdown"
            else ACCOUNTS
            if field == "funds_account_id"
            else {"person", "organization"}
        )
        result.append((field, data[field], kinds))
    return result


def validate_roles(fact, context):
    for field, object_id, kinds in object_roles(fact):
        obj = require_object(fact, context, object_id, kinds, "/data/" + field)
        if field == "fund_id" and obj["data"]["category"] != "money_fund":
            fail("本次货币基金规则不能处理其他类别的投资产品。", "unsupported_business")
        if field == "project_id" and obj["data"]["category"] != "cooperation":
            fail("合作合同须引用合作类别的实际投资项目。", "invalid_input")


def natural_key(fact):
    if fact["kind"] == PREFIX + "contract":
        return "cooperation:" + fact["data"]["project_id"]
    if fact["kind"] == PREFIX + "exit":
        return "cooperation_exit:" + fact["data"]["contract_id"]
    if fact["kind"] != "financing.interest_period":
        return None
    data, ref = fact["data"], fact["data"]["principal_source"]
    return ":".join((ref["business_id"], ref["slot"], data["start_on"], data["end_before"]))


def edge(business_id, producer, relation, *, mode="result", kind="business", first=None, last=None):
    result = {
        "consumer_business_id": business_id,
        "consumer_result_slot": "main",
        "producer_kind": kind,
        "producer_id": producer,
        "producer_public_slot": "main",
        "read_mode": mode,
        "relation_kind": relation,
    }
    if first is not None:
        result.update(period_from=first, period_to=last)
    return result


def direct_dependencies(fact):
    """Final direct references only; no reverse lookup, result slot or amount inference."""
    business_id, data, kind = fact["business_id"], fact["data"], fact["kind"]
    result = [
        edge(business_id, oid, "identity", mode="fact", kind="object")
        for _, oid, _ in object_roles(fact)
    ]
    if kind == "financing.loan_drawdown":
        result.append(edge(business_id, data["contract_id"], "calculation"))
    elif kind == "financing.interest_period":
        source_id = data["principal_source"]["business_id"]
        result.extend(
            [
                edge(business_id, source_id, "calculation"),
                edge(business_id, source_id, "settlement", mode="fact"),
            ]
        )
    elif kind == "investment.money_fund_redemption":
        result.extend(
            edge(business_id, source_id, "cost")
            for source_id in dict.fromkeys(item["source"]["business_id"] for item in data["costs"])
        )
    elif kind.startswith(PREFIX) and kind != PREFIX + "contract":
        result.append(edge(business_id, data["contract_id"], "calculation"))
        if kind in {PREFIX + "collection", PREFIX + "payment"}:
            result.append(edge(business_id, data["source"]["business_id"], "settlement"))
    return result


def dependencies(fact, context):
    business_id, data, kind = fact["business_id"], fact["data"], fact["kind"]
    result = direct_dependencies(fact)
    if kind == "financing.interest_period":
        source_id = data["principal_source"]["business_id"]
        result.extend(
            [
                edge(
                    business_id,
                    source_id,
                    "settlement",
                    mode="collection",
                    first=context.read_context.start_period,
                    last=fact["period"],
                ),
                edge(
                    business_id,
                    source_id,
                    "calculation",
                    mode="collection",
                    first=context.read_context.start_period,
                    last=fact["period"],
                ),
            ]
        )
    elif kind == "investment.money_fund_redemption":
        result.extend(
            {
                **edge(
                    business_id,
                    item["source"]["business_id"],
                    "cost",
                    mode="use",
                    first=fact["period"],
                    last=fact["period"],
                ),
                "producer_public_slot": item["source"]["slot"],
                "resource_components": ["cost"],
            }
            for item in data["costs"]
        )
    if kind in {"financing.owner_funding", PREFIX + "funding", PREFIX + "distribution"}:
        account = context.get_object(data["funds_account_id"])
        if account is not None and account["kind"] == "platform_account":
            result.append(
                edge(
                    business_id,
                    data["funds_account_id"],
                    "identity",
                    mode="collection",
                    kind="object",
                    first=fact["period"],
                    last=fact["period"],
                )
            )
    return result


def publications(fact):
    if fact["kind"] not in {"financing.interest_period", "investment.money_fund_redemption"}:
        return []
    return [
        {
            "business_id": fact["business_id"],
            "result_slot": "main",
            "collection": "pre_tax",
            "period_from": fact["period"],
            "period_to": fact["period"],
        }
    ]
