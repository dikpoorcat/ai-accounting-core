"""Financial-report continuation's only cross-module entry point."""

from copy import deepcopy

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

from ._equations import balance_totals as balance_totals
from ._equations import cash_totals as cash_totals
from ._equations import profit_totals as profit_totals
from ._equations import profit_totals_fen as profit_totals_fen
from ._facts import KIND, fail, validate_fact
from ._planning import evaluate
from ._repository import ReportingRepository


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("ledger.reporting"), ReportingRepository)


def _relation(fact):
    return {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": fact["data"]["opening_package_id"],
        "producer_public_slot": "main",
        "read_mode": "result",
        "relation_kind": "calculation",
    }


class Reporting:
    supported_kinds = (KIND,)

    def declare_relations(self, changes, context: EvaluationContext) -> tuple[DirectRelation, ...]:
        return tuple(
            {
                key: edge[key]
                for key in ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
            }
            for change in changes
            if change["op"] not in {"withdraw", "handoff_out"}
            for edge in (_relation(change.get("fact") or context.get_fact(change["business_id"])),)
        )

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        businesses, edges, publications = [], [], []
        for change in changes:
            business_id = change.get("business_id") or change["fact"]["business_id"]
            item = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": change["op"],
                "complete_result_slots": [],
                "natural_key": None,
            }
            if change["op"] not in {"withdraw", "handoff_out"}:
                fact = change.get("fact") or context.get_fact(business_id)
                if fact is None or fact["business_id"] != business_id:
                    fail("报表接续责任缺少相同业务身份的完整事实。")
                validate_fact(fact, context.read_context.start_period)
                item.update(
                    fact=deepcopy(fact),
                    complete_result_slots=["main"],
                    natural_key=KIND + ":" + fact["period"],
                )
                edges.append(_relation(fact))
                publications.append(
                    {
                        "business_id": business_id,
                        "result_slot": "main",
                        "collection": "pre_tax",
                        "period_from": fact["period"],
                        "period_to": fact["period"],
                    }
                )
            businesses.append(item)
        result = {
            "domain": "reports",
            "businesses": businesses,
            "dependencies": edges,
            "publications": publications,
            "issues": [],
        }
        validate_contract("business-facts.schema.json", "ChangeDescription", result)
        return result

    def evaluate_slot(
        self, business_id: str, result_slot: str, context: EvaluationContext
    ) -> PostingPlan:
        if result_slot != "main":
            fail("报表接续只承接完整main结果槽。")
        fact = context.get_fact(business_id)
        if fact is None:
            fail("当前报表接续事实不存在。", "dependency_conflict")
        return evaluate(fact, context)

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "reports" or plan["issues"]:
            fail("不能核验其他领域或仍有未解问题的报表接续计划。")
        for item in plan["businesses"]:
            if "fact" in item:
                validate_fact(item["fact"], context.read_context.start_period)

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "reports" or plan["issues"]:
            fail("不能保存其他领域或仍有未解问题的报表接续计划。")
        desired = {}
        for item in plan["businesses"]:
            fact = item.get("fact")
            if item["op"] not in {"withdraw", "handoff_out"}:
                validate_fact(fact, uow.start_period)
            desired[item["business_id"]] = (
                None if item["op"] in {"withdraw", "handoff_out"} else fact
            )
        uow.repository("ledger.reporting").replace_current(desired)

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("ledger.reporting").get(ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if (
            query["family"] != "reports"
            or query["company_id"] != read_context.company_id
            or query["period_from"] > query["period_to"]
        ):
            fail("报表接续查询的公司、家族或核算期间不相符。")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("ledger.reporting").query(query)


def create_reporting() -> Reporting:
    return Reporting()
