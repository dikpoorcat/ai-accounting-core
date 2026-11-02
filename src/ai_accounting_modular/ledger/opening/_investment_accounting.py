"""Opening owns the boundary while an injected investment port owns its rules."""

from copy import deepcopy
from typing import Protocol

from ai_accounting_modular.contracts.public import validate_contract

from ._common import fail

INVESTMENT_MEMBERS = frozenset({"investment", "investment_receivable", "cooperation"})
_FIELDS = frozenset(
    {"member_id", "balances", "obligations", "cost_lots", "adopted_quantities", "source_results"}
)


class OpeningInvestmentAccounting(Protocol):
    def opening_investment_members(self, fact, context) -> tuple[dict, ...]: ...


class InvestmentAccounting:
    def __init__(self, port):
        self._port = port

    def require_for(self, fact):
        members = {
            member["member_id"]: member
            for member in fact["data"]["members"]
            if member["data"]["type"] in INVESTMENT_MEMBERS
        }
        if members and self._port is None:
            fail("含投资期初的完整期初包须装配投资专业核算端口。", "unsupported_business")
        return members

    def apply(self, plan, fact, context):
        expected = self.require_for(fact)
        if not expected:
            return
        fragments = self._port.opening_investment_members(fact, context)
        if type(fragments) is not tuple:
            fail("投资期初专业端口须返回完整的成员结果元组。", "invalid_input")
        seen = set()
        for fragment in fragments:
            if type(fragment) is not dict or set(fragment) != _FIELDS:
                fail("投资期初专业结果须完整提供成员、余额、资源及采用依据。", "invalid_input")
            member_id = fragment["member_id"]
            if member_id not in expected or member_id in seen:
                fail("投资期初专业结果包含其他成员或重复成员。", "invalid_input")
            seen.add(member_id)
            if any(type(fragment[field]) is not list for field in _FIELDS - {"member_id"}):
                fail("投资期初专业结果各组成须为完整列表。", "invalid_input")
            for balance in fragment["balances"]:
                validate_contract(
                    "business-facts.schema.json", "AdoptedOpeningBalanceQuantity", balance
                )
                if balance["member_id"] != member_id:
                    fail("投资期初专业余额不能改变原成员身份。", "invalid_input")
            for resource, definition in (("obligations", "Obligation"), ("cost_lots", "CostLot")):
                for value in fragment[resource]:
                    validate_contract("core.schema.json", definition, value)
                    if value["business_id"] != fact["business_id"]:
                        fail("投资期初资源不能属于其他业务。", "invalid_input")
                plan[resource].extend(deepcopy(fragment[resource]))
            plan["basis"]["adopted_quantities"].extend(deepcopy(fragment["balances"]))
            plan["basis"]["adopted_quantities"].extend(deepcopy(fragment["adopted_quantities"]))
            for source in fragment["source_results"]:
                validate_contract("business-facts.schema.json", "ResultSourceRef", source)
                if source not in plan["basis"]["source_results"]:
                    plan["basis"]["source_results"].append(deepcopy(source))
        if seen != expected.keys():
            fail("投资期初专业端口遗漏本单成员，不能保存部分期初。", "invalid_input")
