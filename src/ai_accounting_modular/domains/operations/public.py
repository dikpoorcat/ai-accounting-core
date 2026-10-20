"""Public operating-domain factories, facts, and deterministic slot evaluation."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    BusinessQuery,
    ChangeDescription,
    ChangePlan,
    ContractError,
    EvaluationContext,
    Fact,
    PostingPlan,
    ReadContext,
    UnitOfWork,
    validate_contract,
)
from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

from ._common import issue
from ._expenses import check_expense, expense
from ._relationships import deposit, pass_through, reimbursement
from ._repository import OperationsRepository
from ._roles import object_roles, role_issues
from ._sales import advance, check_advance_tax_status, service_sale

_CALCULATORS = {
    "operations.expense": expense,
    "operations.deposit": deposit,
    "operations.pass_through": pass_through,
    "operations.reimbursement": reimbursement,
    "operations.service_sale": service_sale,
    "operations.advance": advance,
}


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("domains.operations"), OperationsRepository)


def _main_object(fact):
    roles = object_roles(fact)
    return roles[0].object_id if roles else None


def _references(fact):
    data, kind = fact["data"], fact["kind"]
    if kind == "operations.reimbursement":
        return [x["source"] for x in data.get("allocations", data.get("sources", ()))]
    if "vat" in data:
        refs = [{"business_id": data["vat"]["policy_id"], "slot": "main"}]
        if data.get("tax_point", {}).get("type") == "derived":
            refs.append({"business_id": data["tax_point"]["tax_point_id"], "slot": "main"})
        return refs
    return []


def _validate_fact(fact):
    if (
        fact.get("kind") == "operations.reimbursement"
        and fact.get("data", {}).get("type") == "personal_payment"
        and "effective_on" not in fact
    ):
        raise ContractError(
            "needs_information",
            "真实个人付款需要实际付款日。",
            fact_issues=[
                issue(fact, "effective_on", "个人实际垫付必须提供真实日期。", precision=("day",))
            ],
        )
    validate_contract("business-facts.schema.json", "Fact", fact)


class Operations:
    supported_kinds = tuple(_CALCULATORS)

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        responsibilities, dependencies, issues = [], [], []
        for change in changes:
            business_id, op = (
                change.get("business_id", change.get("fact", {}).get("business_id")),
                change["op"],
            )
            responsibility = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": op,
                "complete_result_slots": [],
            }
            if op in {"withdraw", "handoff_out"}:
                responsibilities.append(responsibility)
                continue
            fact = change.get("fact") or context.get_fact(business_id)
            if fact is None:
                raise ContractError("invalid_input", "经营核算缺少完整业务事实。")
            _validate_fact(fact)
            if fact["business_id"] != business_id:
                raise ContractError("invalid_input", "责任业务身份与事实不一致。")
            if fact["kind"] not in _CALCULATORS:
                raise ContractError("unsupported_business", f"经营模块尚未实现：{fact['kind']}")
            if fact["kind"] == "operations.expense":
                check_expense(fact)
            elif fact["kind"] == "operations.advance":
                check_advance_tax_status(fact)
            responsibility.update(fact=deepcopy(fact), complete_result_slots=["main"])
            responsibilities.append(responsibility)
            roles = object_roles(fact)
            issues.extend(role_issues(fact, context, roles))
            for object_id in dict.fromkeys(ref.object_id for ref in roles):
                dependencies.append(
                    {
                        "consumer_business_id": business_id,
                        "consumer_result_slot": "main",
                        "producer_kind": "object",
                        "producer_id": object_id,
                        "producer_public_slot": "main",
                        "read_mode": "fact",
                        "relation_kind": "identity",
                    }
                )
            for ref in _references(fact):
                policy_only = (
                    fact["kind"] == "operations.advance"
                    and fact["data"]["side"] == "customer"
                    and fact["data"]["tax_status"] == "not_due"
                )
                dependencies.append(
                    {
                        "consumer_business_id": business_id,
                        "consumer_result_slot": "main",
                        "producer_kind": "business",
                        "producer_id": ref["business_id"],
                        "producer_public_slot": "main",
                        "read_mode": "fact" if policy_only else "result",
                        "relation_kind": "policy" if policy_only else "calculation",
                    }
                )
        result = {
            "domain": "operations",
            "businesses": responsibilities,
            "dependencies": dependencies,
            "issues": issues,
        }
        validate_contract("business-facts.schema.json", "ChangeDescription", result)
        return result

    def evaluate_slot(
        self, business_id: str, result_slot: str, context: EvaluationContext
    ) -> PostingPlan:
        if result_slot != "main":
            raise ContractError("invalid_input", "经营业务只支持main完整结果槽。")
        fact = context.get_fact(business_id)
        if fact is None:
            raise ContractError("dependency_conflict", "经营业务当前事实不存在。")
        _validate_fact(fact)
        calculator = _CALCULATORS.get(fact["kind"])
        if calculator is None:
            raise ContractError("unsupported_business", f"经营模块尚未实现：{fact['kind']}")
        issues = role_issues(fact, context, object_roles(fact))
        if issues:
            invalid_kind = any(item["code"] == "incompatible_object_kind" for item in issues)
            raise ContractError(
                "invalid_input" if invalid_kind else "needs_information",
                "经营业务引用的对象不符合其业务角色。",
                fact_issues=issues,
            )
        plan = calculator(fact, context)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        return plan

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "operations" or plan["issues"]:
            raise ContractError("invalid_input", "不能保存其他领域或仍有核算问题的经营变更。")
        repo = uow.repository("domains.operations")
        for responsibility in plan["businesses"]:
            if responsibility["op"] in {"withdraw", "handoff_out"}:
                repo.remove(responsibility["business_id"])
            else:
                fact = responsibility["fact"]
                if fact["kind"] not in _CALCULATORS:
                    raise ContractError("unsupported_business", "不能保存尚未实现的经营事实。")
                repo.save(fact, _main_object(fact))

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("domains.operations").get(ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if query["company_id"] != read_context.company_id or query["family"] != "operations":
            raise ContractError("invalid_input", "经营查询的公司或领域不一致。")
        if query["period_from"] > query["period_to"]:
            raise ContractError("invalid_input", "查询起月不能晚于止月。")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("domains.operations").query(query)


def create_operations() -> Operations:
    return Operations()
