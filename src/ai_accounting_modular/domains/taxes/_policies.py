"""Explicitly sourced parameters; no policy is picked from the computer clock."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_rate

from ._common import adopt_source, fact_for, missing, month_end, plan_for, reject, result_for

POLICIES = (
    "taxes.vat_policy",
    "taxes.surtax_policy",
    "taxes.used_asset_vat_policy",
    "taxes.income_tax_policy",
)


def check_policy(fact):
    data, identity = fact["data"], fact["data"]["policy"]
    if identity.get("effective_to", "9999-12-31") < identity["effective_from"]:
        reject("政策有效止日不能早于起日。", "invalid_input")
    if fact["kind"] in ("taxes.vat_policy", "taxes.used_asset_vat_policy"):
        if parse_rate(data["payable_rate"]) > parse_rate(data["tax_base_rate"]):
            reject("实纳率不能高于该政策明确的计税基础率。", "invalid_input")


def policy(fact, context):
    check_policy(fact)
    identity = fact["data"]["policy"]
    values = {
        key: identity[key]
        for key in ("official_url", "effective_from", "effective_to")
        if key in identity
    }
    values["policy_version"] = identity["version"]
    plan = plan_for(fact, context, values)
    plan["rule_refs"][0]["official_urls"] = [identity["official_url"]]
    return plan


def covers(source, start, end):
    identity = source["data"]["policy"]
    return identity["effective_from"] <= start and identity.get("effective_to", "9999-12-31") >= end


def adopt_policy(plan, fact, context, business_id, kind, start_month, end_month, *, required=True):
    if business_id is None:
        if required:
            missing(
                fact, "/data/" + kind.removeprefix("taxes.") + "_id", "计算参考需要明确的政策来源。"
            )
        return None
    if not required and context.get_fact(business_id) is None:
        return None
    source = fact_for(fact, context, business_id, kind, "/data/policy_id")
    check_policy(source)
    if not covers(source, start_month + "-01", month_end(end_month)):
        if required:
            missing(fact, "/data/policy_id", "政策必须完整覆盖本次计算期间。", precision=("day",))
        return None
    result = result_for(fact, context, business_id, kind, "/data/policy_id")
    adopt_source(plan, result.source)
    row = {
        "policy_business_id": business_id,
        "policy_kind": kind,
        "parameters": deepcopy(source["data"]),
    }
    if row not in plan["basis"]["policies"]:
        plan["basis"]["policies"].append(row)
    rule = plan["rule_refs"][0]
    if business_id not in rule["policy_business_ids"]:
        rule["policy_business_ids"].append(business_id)
    url = source["data"]["policy"]["official_url"]
    if url not in rule["official_urls"]:
        rule["official_urls"].append(url)
    return source["data"]
