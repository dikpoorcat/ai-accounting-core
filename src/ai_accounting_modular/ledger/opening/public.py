"""Opening's domain port; sourced initial balances share normal formal adoption."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    BusinessQuery,
    ChangeDescription,
    ChangePlan,
    EvaluationContext,
    Fact,
    OpeningAssetAccounting,
    PostingPlan,
    ReadContext,
    UnitOfWork,
    format_fen,
    parse_fen,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from ._asset_accounting import AssetAccounting
from ._common import base_plan, fail, total
from ._facts import dependencies, validate_fact, validate_roles
from ._members import CALCULATORS, asset_balances
from ._origin import is_zero_origin as is_zero_origin
from ._repository import OpeningRepository


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("ledger.opening"), OpeningRepository)


class Opening:
    supported_kinds = ("opening.package",)

    def __init__(self, *, asset_accounting: OpeningAssetAccounting | None = None):
        self._asset_accounting = AssetAccounting(asset_accounting)

    def declare_relations(self, changes, context: EvaluationContext):
        return tuple(
            edge
            for change in changes
            if change["op"] not in {"withdraw", "handoff_out"}
            for edge in dependencies(change.get("fact") or context.get_fact(change["business_id"]))
        )

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        businesses, edges, published, issues = [], [], [], []
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
                    fail("期初责任缺少相同业务身份的完整事实。", "invalid_input")
                validate_fact(fact, context)
                validate_roles(fact, context)
                assets = self._asset_accounting.describe(fact, context)
                responsibility.update(
                    fact=deepcopy(fact),
                    complete_result_slots=["main", *assets["complete_result_slots"]],
                    natural_key="opening.package:" + context.read_context.start_period,
                )
                edges.extend(dependencies(fact))
                edges.extend(assets["dependencies"])
                published.extend(assets["publications"])
                issues.extend(assets["issues"])
                published.append(
                    {
                        "business_id": business_id,
                        "result_slot": "main",
                        "collection": "pre_tax",
                        "period_from": fact["period"],
                        "period_to": fact["period"],
                    }
                )
            businesses.append(responsibility)
        result = {
            "domain": "opening",
            "businesses": businesses,
            "dependencies": edges,
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
            fail("当前期初事实不存在。")
        validate_fact(fact, context)
        validate_roles(fact, context)
        if result_slot != "main":
            return self._asset_accounting.evaluate(fact, result_slot, context)
        cards = self._asset_accounting.cards(fact, context)
        plan = base_plan(fact, context)
        for member in sorted(fact["data"]["members"], key=lambda item: item["member_id"]):
            if member["data"]["type"] == "asset":
                asset_balances(plan, member, cards[member["data"]["asset_id"]])
            else:
                CALCULATORS[member["data"]["type"]](plan, member, context)
        balances = [
            q for q in plan["basis"]["adopted_quantities"] if q["type"] == "opening_balance"
        ]
        debit = total(parse_fen(row["debit_fen"]) for row in balances)
        credit = total(parse_fen(row["credit_fen"]) for row in balances)
        if debit != credit:
            fail("期初资产、负债及权益不平衡，须查明真实组成，不能自动补差。")
        plan["target"]["values"].update(debit_fen=format_fen(debit), credit_fen=format_fen(credit))
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        return plan

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "opening" or plan["issues"]:
            fail("期初不能核验其他领域或仍有未解问题的计划。", "invalid_input")
        for responsibility in plan["businesses"]:
            if "fact" in responsibility:
                validate_fact(responsibility["fact"], context)
                validate_roles(responsibility["fact"], context)
                self._asset_accounting.require_for(responsibility["fact"])
        self._asset_accounting.validate(plan, context)

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "opening" or plan["issues"]:
            fail("不能保存其他领域或仍有核算问题的期初计划。", "invalid_input")
        desired = {}
        for responsibility in plan["businesses"]:
            fact = responsibility.get("fact")
            if responsibility["op"] not in {"withdraw", "handoff_out"}:
                validate_fact(fact)
            desired[responsibility["business_id"]] = (
                None if responsibility["op"] in {"withdraw", "handoff_out"} else fact
            )
        uow.repository("ledger.opening").replace_current(desired)

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("ledger.opening").get(ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if (
            query["family"] != "opening"
            or query["company_id"] != read_context.company_id
            or query["period_from"] > query["period_to"]
        ):
            fail("期初查询的公司、家族或核算期间不相符。", "invalid_input")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("ledger.opening").query(query)


def create_opening(*, asset_accounting: OpeningAssetAccounting | None = None) -> Opening:
    return Opening(asset_accounting=asset_accounting)
