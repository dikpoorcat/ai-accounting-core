"""The supported cooperation profile uses ordinary plans and public identities."""

from uuid import NAMESPACE_URL, uuid5

from ._common import STANDARD_URL, adopt, base_plan, fact_error

PREFIX = "investment.cooperation_"
KINDS = tuple(
    PREFIX + name
    for name in (
        "contract",
        "funding",
        "collection",
        "payment",
        "operator_transfer",
        "distribution",
        "exit",
    )
)
AGENCIES = {PREFIX + "collection", PREFIX + "payment"}


def actual_on(fact):
    return fact["data"].get("actual_on", fact.get("effective_on"))


def event_order(fact):
    day = actual_on(fact)
    return fact["period"], day is None, day or "", fact["business_id"]


def stable_id(business_id, purpose):
    return str(uuid5(NAMESPACE_URL, "investment.cooperation:" + business_id + ":" + purpose))


def cooperation_plan(fact, context, values, *, accounting_input=None):
    plan = base_plan(fact, context, values, accounting_input=accounting_input)
    plan["basis"]["policies"] = [
        {
            "policy_id": "cooperation.cash_labor",
            "revision": 1,
            "rules": [
                {
                    "rule_id": "cooperation.direct_joint_operation_cash_labor",
                    "version": "1",
                    "source_url": STANDARD_URL,
                    "effective_from": "2013-01-01",
                    "effective_to": None,
                }
            ],
        }
    ]
    return plan


def require_source_period(fact, result, field, meaning):
    if fact["period"] < result.basis["period"]:
        fact_error(
            fact,
            field,
            f"本次真实事项所属月{fact['period']}早于{meaning}{result.basis['period']}；"
            "请核对实际事项及来源的正确生效月份，不能自动提前合同生效或改动实际收付日。",
            error="dependency_conflict",
            precision=("month", "source"),
            code="inconsistent_fact",
        )


def terms(fact, context):
    contract_id = fact["data"]["contract_id"]
    result = context.get_result(contract_id, "main")
    if result is None:
        fact_error(
            fact,
            "/data/contract_id",
            "所引用的合作合同尚无正式结果，请检查该合同的登记和核算结果。",
            error="dependency_conflict",
            code="result_not_ready",
            precision=("source",),
        )
    if result.target["kind"] != PREFIX + "contract":
        fact_error(
            fact,
            "/data/contract_id",
            "合作事项引用的来源不是受支持的合作合同。",
            error="invalid_input",
            code="incompatible_business_kind",
        )
    require_source_period(fact, result, "/data/contract_id", "合作合同生效月")
    return result


def adopt_result(plan, result):
    adopt(plan, result.source)
