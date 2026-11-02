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
    LedgerScope,
    PageQuery,
    PostingPlan,
    ReadContext,
    ReadPage,
    UnitOfWork,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import _investment_description, _investment_events, _investment_plans
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
from ._read_cooperation import positions as cooperation_positions
from ._read_funds import activity as fund_activity
from ._read_funds import query_events as fund_events
from ._read_types import CooperationPosition as CooperationPosition
from ._read_types import CooperationPositions as CooperationPositions
from ._read_types import MoneyFundActivity as MoneyFundActivity
from ._read_types import MoneyFundEvent as MoneyFundEvent
from ._read_types import MoneyFundProduct as MoneyFundProduct
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


class _FactOwner:
    def __init__(self, family: Literal["financing", "investment"], *, ledger=None, identity=None):
        self.family = family
        self._ledger, self._identity = ledger, identity
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
        if self.family == "investment":
            from ._investment_description_inputs import DescriptionInputs

            context = DescriptionInputs(context)
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
                result_slots = self._result_slots(fact, context)
                responsibility.update(
                    fact=deepcopy(fact),
                    complete_result_slots=result_slots,
                    natural_key=natural_key(fact),
                )
                edges.extend(dependencies(fact, context))
                edges.extend(self._extra_dependencies(fact, context, result_slots))
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
        result = self._evaluate(fact, result_slot, context)
        validate_contract("business-facts.schema.json", "PostingPlan", result)
        return result

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != self.family or plan["issues"]:
            fail("融资投资不能核验其他领域或仍有未解问题的计划。", "invalid_input")
        self._validate_events(plan, context)

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != self.family or plan["issues"]:
            fail("不能保存其他领域或有核算问题的融资投资计划。", "invalid_input")
        repository = uow.repository("domains.financing")
        for responsibility in sorted(
            plan["businesses"],
            key=lambda item: (
                item.get("fact", {}).get("kind") not in {PREFIX + "contract", "investment.terms"}
            ),
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


class Financing(_FactOwner):
    def __init__(self):
        super().__init__("financing")

    def _result_slots(self, fact, context):
        return ["main"]

    def _extra_dependencies(self, fact, context, slots):
        return []

    def _evaluate(self, fact, result_slot, context):
        if result_slot != "main":
            fail("融资业务只承担主结果槽。", "invalid_input")
        return _CALCULATORS[fact["kind"]](fact, context)

    def _validate_events(self, plan, context):
        validate_interest(plan, context)


class Investment(_FactOwner):
    """The investment owner shares persistence, never the financing rule switch."""

    def __init__(self, *, ledger=None, identity=None):
        super().__init__("investment", ledger=ledger, identity=identity)

    def _result_slots(self, fact, context):
        if fact["kind"].startswith(PREFIX):
            return cooperation_slots(fact, context)
        return _investment_description.slots(fact, context)

    def _extra_dependencies(self, fact, context, slots):
        if fact["kind"].startswith(PREFIX):
            return cooperation_dependencies(fact, context, slots)
        return []

    def _evaluate(self, fact, result_slot, context):
        if fact["kind"] == "investment.unit_change_basis" and result_slot == "main":
            from ._investment_unit_basis import plan

            return plan(fact, context)
        if fact["kind"] == "investment.terms":
            if result_slot.startswith("position:"):
                return _investment_plans.position_plan(fact, result_slot, context)
            key = (
                result_slot.removeprefix("authorization:")
                if result_slot.startswith("authorization:")
                else None
            )
            if result_slot != "main" and key is None:
                fail("投资条款未承担该授权结果槽。", "invalid_input")
            return _investment_plans.terms(fact, context, authorization=key)
        if fact["kind"] in _investment_events.EVENT_KINDS and result_slot == "main":
            return _investment_plans.event_plan(fact, context)
        if fact["kind"] == PREFIX + "contract" and result_slot.startswith("position:"):
            period = result_slot.removeprefix("position:")
            validate_contract("core.schema.json", "Month", period)
            first = max(fact["period"], context.read_context.start_period)
            if not first <= period <= max(first, context.evaluation_period_to):
                fail("合作位置不在合同生效、开账和明确工作月份范围内。", "invalid_input")
            return position_plan(fact, result_slot, context)
        if result_slot != "main":
            fail("投资业务未承担请求的正式结果槽。", "invalid_input")
        return _CALCULATORS[fact["kind"]](fact, context)

    def _validate_events(self, plan, context):
        contracts, holdings = set(), set()
        for responsibility in plan["businesses"]:
            fact = responsibility.get("fact")
            if fact is None:
                continue
            if fact["kind"].startswith(PREFIX):
                contracts.add(
                    fact["business_id"]
                    if fact["kind"] == PREFIX + "contract"
                    else fact["data"]["contract_id"]
                )
            elif fact["kind"] in _investment_events.POSITION_KINDS or (
                fact["kind"] == "investment.terms"
                and any(
                    slot.startswith("position:") for slot in responsibility["complete_result_slots"]
                )
            ):
                holdings.add(
                    fact["business_id"]
                    if fact["kind"] == "investment.terms"
                    else fact["data"]["terms_id"]
                )
        for contract_id in sorted(contracts):
            fact = context.get_fact(contract_id)
            if fact is None or fact["kind"] != PREFIX + "contract":
                fail("合作候选计划缺少完整当前合同。")
            validate_contract_events(fact, context)
        for terms_id in sorted(holdings):
            result = context.get_result(terms_id, "main")
            if result is None:
                fail("投资候选集合缺少采用的持仓条款。")
            from ._investment_candidate import inventory

            fact = context.get_fact(terms_id)
            inventory(fact, result, context)

    def opening_investment_members(self, fact, context):
        from ._investment_opening import opening_investment_members

        return opening_investment_members(fact, context)

    def investment_activity(self, scope, read_context):
        from ._read_investments import activity

        if self._ledger is None or self._identity is None:
            fail("投资活动读取未装配正式账务和产品端口。", "unsupported_business")
        return activity(self, scope, read_context)

    def money_fund_activity(
        self, scope: LedgerScope, read_context: ReadContext
    ) -> MoneyFundActivity:
        if self.family != "investment" or self._ledger is None or self._identity is None:
            fail("基金活动读取未装配投资、正式账务及产品端口。", "unsupported_business")
        return fund_activity(self, scope, read_context)

    def cooperation_positions(
        self, scope: LedgerScope, read_context: ReadContext, *, contract_ids: tuple[str, ...] = ()
    ) -> CooperationPositions:
        if self.family != "investment" or self._ledger is None:
            fail("合作位置读取未装配投资及正式账务端口。", "unsupported_business")
        return cooperation_positions(self, scope, read_context, contract_ids)

    def query_money_fund_events(
        self, scope: LedgerScope, page: PageQuery, read_context: ReadContext
    ) -> ReadPage[MoneyFundEvent]:
        if self.family != "investment" or self._ledger is None:
            fail("基金事件读取未装配投资及正式账务端口。", "unsupported_business")
        return fund_events(self, scope, page, read_context)


def create_financing() -> Financing:
    return Financing()


def create_investment(*, ledger=None, identity=None) -> Investment:
    return Investment(ledger=ledger, identity=identity)
