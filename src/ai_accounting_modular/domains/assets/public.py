"""Public asset-owner ports; asset facts produce controlled accounting plans."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    AdoptedAssetQuantity,
    BusinessQuery,
    ChangeDescription,
    ChangePlan,
    ContractError,
    DirectRelation,
    EvaluationContext,
    Fact,
    LedgerScope,
    OpeningAssetSlots,
    PageQuery,
    PostingPlan,
    ReadContext,
    ReadPage,
    ResourceCutoff,
    UnitOfWork,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from . import _reading
from ._common import assets_in, finalize_plan, reject
from ._consumption import consumption, slot_for
from ._description import DescriptionContext, activation_months, direct_dependencies, publications
from ._description import dependencies as dependency_edges
from ._facts import CALCULATORS, natural_key, role_issues, validate_fact
from ._inputs import AssetInputs, batches, reference_key
from ._opening import cards as opening_cards
from ._opening import describe as describe_opening
from ._opening import evaluate as evaluate_opening
from ._opening import validate as validate_opening
from ._origins import require_existing_origins, validate_origins
from ._read_types import (
    AssetActivity,
    AssetCard,
    AssetCardQuery,
    AssetExit,
    AssetLedgerAmounts,
    AssetPaymentSummary,
    AssetProjectCost,
    AssetTypeSummary,
    ProjectCostPage,
    ProjectCostQuery,
    ProjectCostTotals,
)
from ._repository import AssetsRepository

__all__ = [
    "Assets",
    "AssetActivity",
    "AssetCard",
    "AssetCardQuery",
    "AssetExit",
    "AssetLedgerAmounts",
    "AssetPaymentSummary",
    "AssetProjectCost",
    "AssetTypeSummary",
    "ProjectCostPage",
    "ProjectCostQuery",
    "ProjectCostTotals",
    "create_assets",
    "repository_factory",
]


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("domains.assets"), AssetsRepository)


class Assets:
    supported_kinds = tuple(CALCULATORS)

    def __init__(self, *, ledger=None, identity=None):
        self._ledger, self._identity = ledger, identity

    def asset_activity(self, scope: LedgerScope, read_context: ReadContext) -> AssetActivity:
        """Full synchronous projection; UI pagination belongs to its saved read model."""
        return _reading.asset_activity(self, scope, read_context)

    def query_cards(
        self, scope: LedgerScope, query: AssetCardQuery, page: PageQuery, read_context: ReadContext
    ) -> ReadPage[AssetCard]:
        return _reading.query_cards(self, scope, query, page, read_context)

    def query_project_costs(
        self,
        scope: LedgerScope,
        query: ProjectCostQuery,
        page: PageQuery,
        read_context: ReadContext,
    ) -> ProjectCostPage:
        return _reading.query_project_costs(self, scope, query, page, read_context)

    def declare_relations(self, changes, context: EvaluationContext) -> tuple[DirectRelation, ...]:
        return tuple(
            {
                key: edge[key]
                for key in ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
            }
            for change in changes
            if change["op"] not in {"withdraw", "handoff_out"}
            for edge in direct_dependencies(
                change.get("fact") or context.get_fact(change["business_id"])
            )
        )

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        context = DescriptionContext(context, changes)
        businesses, dependencies, issues, published = [], [], [], []
        for change in changes:
            business_id = change.get("business_id", change.get("fact", {}).get("business_id"))
            responsibility = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": change["op"],
                "complete_result_slots": [],
                "natural_key": None,
            }
            if change["op"] in {"withdraw", "handoff_out"}:
                businesses.append(responsibility)
                continue
            fact = change.get("fact") or context.get_fact(business_id)
            if fact is None or fact["business_id"] != business_id:
                reject("资产变更缺少相同业务身份的完整事实。", "invalid_input")
            found = role_issues(fact, context)
            issues.extend(found)
            if (
                not found
                and change["op"] in {"replace", "restore", "recalculate"}
                and fact["kind"] in {"assets.activation", "assets.disposal"}
            ):
                require_existing_origins(fact, context)
            slots = ["main"]
            if fact["kind"] == "assets.activation" and not found:
                slots.extend(slot_for(month) for month in activation_months(fact, context))
            responsibility.update(
                fact=deepcopy(fact), complete_result_slots=slots, natural_key=natural_key(fact)
            )
            businesses.append(responsibility)
            if not found:
                dependencies.extend(dependency_edges(fact, slots, context))
                published.extend(publications(fact, slots))
        result = {
            "domain": "assets",
            "businesses": businesses,
            "dependencies": dependencies,
            "publications": published,
            "issues": issues,
        }
        validate_contract("business-facts.schema.json", "ChangeDescription", result)
        return result

    def evaluate_slot(
        self, business_id: str, result_slot: str, context: EvaluationContext
    ) -> PostingPlan:
        fact = context.get_fact(business_id)
        if fact is None:
            reject("资产当前事实不存在。")
        validate_fact(fact)
        context = AssetInputs(context, facts=(fact,))
        context.load_assets(item["asset_id"] for item in assets_in(fact))
        issues = role_issues(fact, context)
        if issues:
            raise ContractError(
                "invalid_input"
                if any(x["code"] == "incompatible_object_kind" for x in issues)
                else "needs_information",
                "资产引用对象不符合业务角色。",
                fact_issues=issues,
            )
        if result_slot == "main":
            plan = CALCULATORS[fact["kind"]](fact, context)
        elif fact["kind"] == "assets.activation" and result_slot.startswith("depreciation:"):
            month = result_slot[len("depreciation:") :]
            validate_contract("core.schema.json", "Month", month)
            if month > context.evaluation_period_to:
                reject("不能在明确的核算工作范围之外提前摊折。", "invalid_input")
            plan = consumption(fact, month, context)
        else:
            reject("资产事实不承担该结果槽。", "invalid_input")
        finalize_plan(plan)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        return plan

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "assets":
            reject("资产模块不能核验其他领域计划。", "invalid_input")
        facts = [item["fact"] for item in plan["businesses"] if "fact" in item]
        context = AssetInputs(context)
        context.load_assets(item["asset_id"] for fact in facts for item in assets_in(fact))
        validate_origins(context, (card["asset_id"] for fact in facts for card in assets_in(fact)))
        # All calculations used immutable definitions. Only now inspect complete
        # candidate capacities, including siblings, replacements and withdrawals.
        by_period = {}
        for posting in plan["plans"]:
            refs = by_period.setdefault(posting["accounting_period"], {})
            refs.update(
                (reference_key(item["source"]), item["source"])
                for item in posting["cost_consumptions"]
            )
        for period, refs in sorted(by_period.items()):
            for batch in batches(refs.values()):
                states = context.resource_states(batch, "cost", ResourceCutoff(period))
                if (
                    len(states) != len(batch)
                    or {reference_key(state.definition.reference) for state in states}
                    != {reference_key(ref) for ref in batch}
                    or any(state.definition.kind != "cost" for state in states)
                ):
                    reject("候选成本核验缺失、重复或返回了其他身份。")
                if any(state.remaining_fen < 0 for state in states):
                    reject("同一成本已被本单或此前业务重复消耗。")

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "assets" or plan["issues"]:
            reject("不能保存其他领域或仍有核算问题的资产计划。", "invalid_input")
        repo = uow.repository("domains.assets")
        for responsibility in plan["businesses"]:
            if responsibility["op"] in {"withdraw", "handoff_out"}:
                repo.remove(responsibility["business_id"])
            else:
                validate_fact(responsibility["fact"])
                repo.save(responsibility["fact"])

    def describe_opening_assets(self, fact: Fact, context: EvaluationContext) -> OpeningAssetSlots:
        return describe_opening(fact, context)

    def opening_asset_cards(
        self, fact: Fact, context: EvaluationContext
    ) -> tuple[AdoptedAssetQuantity, ...]:
        return opening_cards(fact, context)

    def evaluate_opening_asset_slot(
        self, fact: Fact, result_slot: str, context: EvaluationContext
    ) -> PostingPlan:
        return evaluate_opening(fact, result_slot, context)

    def validate_opening_asset_candidate(
        self, plan: ChangePlan, context: EvaluationContext
    ) -> None:
        validate_opening(plan, context)

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("domains.assets").get(ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if (
            query["company_id"] != read_context.company_id
            or query["family"] != "assets"
            or query["period_from"] > query["period_to"]
        ):
            reject("资产查询的公司、领域或月份范围不一致。", "invalid_input")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("domains.assets").query(query)


def create_assets(*, ledger=None, identity=None) -> Assets:
    return Assets(ledger=ledger, identity=identity)
