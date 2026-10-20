"""Public financing and investment factories, with one data owner and no private cross-reads."""

from copy import deepcopy
from typing import Literal

from ai_accounting_modular.contracts.public import (
    BusinessQuery,
    ChangeDescription,
    ChangePlan,
    DirectRelation,
    EvaluationContext,
    Fact,
    PostingPlan,
    ReadContext,
    UnitOfWork,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from ._changes import (
    FINANCING_KINDS,
    INVESTMENT_KINDS,
    dependencies,
    direct_dependencies,
    natural_key,
    publications,
    validate_fact,
    validate_roles,
)
from ._common import fail
from ._cooperation_agency import agency
from ._cooperation_checks import validate_contract_events
from ._cooperation_common import PREFIX
from ._cooperation_description import dependencies as cooperation_dependencies
from ._cooperation_description import slots as cooperation_slots
from ._cooperation_plans import actual_event
from ._cooperation_plans import contract as cooperation_contract
from ._cooperation_position import position_plan
from ._funds import purchase, redemption
from ._interest import interest, validate_interest
from ._loans import contract, drawdown, owner_funding
from ._repository import FinancingRepository

_CALCULATORS = {
    "financing.owner_funding": owner_funding,
    "financing.loan_contract": contract,
    "financing.loan_drawdown": drawdown,
    "financing.interest_period": interest,
    "investment.money_fund_purchase": purchase,
    "investment.money_fund_redemption": redemption,
    PREFIX + "contract": cooperation_contract,
    PREFIX + "funding": actual_event,
    PREFIX + "collection": agency,
    PREFIX + "payment": agency,
    PREFIX + "operator_transfer": actual_event,
    PREFIX + "distribution": actual_event,
    PREFIX + "exit": actual_event,
}


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("domains.financing"), FinancingRepository)


class Financing:
    def __init__(self, family: Literal["financing", "investment"]):
        self.family = family
        self.supported_kinds = FINANCING_KINDS if family == "financing" else INVESTMENT_KINDS

    def declare_relations(self, changes, context) -> tuple[DirectRelation, ...]:
        result = []
        for change in changes:
            if change["op"] in {"withdraw", "handoff_out"}:
                continue
            business_id = change.get("business_id") or change["fact"]["business_id"]
            fact = change.get("fact") or context.get_fact(business_id)
            if fact is None:
                fail("融资投资关系声明缺少完整当前事实。", "invalid_input")
            result.extend(direct_dependencies(fact))
        return tuple(
            {
                key: edge[key]
                for key in ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
            }
            for edge in result
        )

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        businesses, edges, published = [], [], []
        for change in changes:
            business_id = change.get("business_id") or change["fact"]["business_id"]
            responsibility = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": change["op"],
                "complete_result_slots": [],
                "natural_key": None,
            }
            if change["op"] not in {"withdraw", "handoff_out"}:
                fact = change.get("fact") or context.get_fact(business_id)
                if fact is None or fact["business_id"] != business_id:
                    fail("融资投资责任缺少相符的完整当前事实。", "invalid_input")
                validate_fact(fact, self.family)
                validate_roles(fact, context)
                result_slots = (
                    cooperation_slots(fact, context)
                    if fact["kind"].startswith(PREFIX)
                    else ["main"]
                )
                responsibility.update(
                    fact=deepcopy(fact),
                    complete_result_slots=result_slots,
                    natural_key=natural_key(fact),
                )
                edges.extend(dependencies(fact, context))
                if fact["kind"].startswith(PREFIX):
                    edges.extend(cooperation_dependencies(fact, context, result_slots))
                published.extend(publications(fact))
            businesses.append(responsibility)
        description = {
            "domain": self.family,
            "businesses": businesses,
            "dependencies": edges,
            "publications": published,
            "issues": [],
        }
        validate_contract("business-facts.schema.json", "ChangeDescription", description)
        return description

    def evaluate_slot(
        self, business_id: str, result_slot: str, context: EvaluationContext
    ) -> PostingPlan:
        fact = context.get_fact(business_id)
        if fact is None:
            fail("融资投资当前事实不存在。")
        validate_fact(fact, self.family)
        validate_roles(fact, context)
        if fact["kind"] == PREFIX + "contract" and result_slot.startswith("position:"):
            period = result_slot.removeprefix("position:")
            validate_contract("core.schema.json", "Month", period)
            first = max(fact["period"], context.read_context.start_period)
            if not first <= period <= max(first, context.evaluation_period_to):
                fail("合作位置不在合同生效、开账和明确工作月份范围内。", "invalid_input")
            result = position_plan(fact, result_slot, context)
        elif result_slot == "main":
            result = _CALCULATORS[fact["kind"]](fact, context)
        else:
            fail("该融资投资业务不承担所请求的完整结果槽。", "invalid_input")
        validate_contract("business-facts.schema.json", "PostingPlan", result)
        return result

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != self.family or plan["issues"]:
            fail("融资投资不能核验其他领域或仍有未解问题的计划。", "invalid_input")
        if self.family == "financing":
            validate_interest(plan, context)
        else:
            contract_ids = {
                fact["business_id"]
                if fact["kind"] == PREFIX + "contract"
                else fact["data"]["contract_id"]
                for responsibility in plan["businesses"]
                if (fact := responsibility.get("fact")) is not None
                and fact["kind"].startswith(PREFIX)
            }
            for contract_id in sorted(contract_ids):
                fact = context.get_fact(contract_id)
                if fact is None or fact["kind"] != PREFIX + "contract":
                    fail("合作候选计划缺少完整当前合同。")
                validate_contract_events(fact, context)

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != self.family or plan["issues"]:
            fail("不能保存其他领域或有核算问题的融资投资计划。", "invalid_input")
        repository = uow.repository("domains.financing")
        for responsibility in sorted(
            plan["businesses"],
            key=lambda item: item.get("fact", {}).get("kind") != PREFIX + "contract",
        ):
            if responsibility["op"] in {"withdraw", "handoff_out"}:
                repository.remove(responsibility["business_id"], self.family)
            else:
                validate_fact(responsibility["fact"], self.family)
                repository.save(responsibility["fact"], self.family)

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("domains.financing").get(self.family, ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if (
            query["family"] != self.family
            or query["company_id"] != read_context.company_id
            or query["period_from"] > query["period_to"]
        ):
            fail("融资投资查询的公司、家族或期间不一致。", "invalid_input")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("domains.financing").query(query)


def create_financing() -> Financing:
    return Financing("financing")


def create_investment() -> Financing:
    return Financing("investment")
