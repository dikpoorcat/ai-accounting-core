"""Public people-domain ports; callers submit facts, never journal lines."""

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

from ._common import problem
from ._contributions import actual, check_actual, check_policy, employment, policy
from ._remuneration import remuneration
from ._repository import PeopleRepository

_CALCULATORS = {
    "people.employment": employment,
    "people.contribution_policy": policy,
    "people.contribution_actual": actual,
    "people.remuneration": remuneration,
}


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("domains.people"), PeopleRepository)


def _roles(fact):
    data = fact["data"]
    roles = []
    for field in ("employee_id", "person_id"):
        if field in data:
            roles.append(("/data/" + field, data[field], "person"))
    if data.get("destination", {}).get("type") == "project":
        roles.append(("/data/destination/project_id", data["destination"]["project_id"], "project"))
    return roles


def _role_issues(fact, context):
    issues = []
    for field, object_id, kind in _roles(fact):
        obj = context.get_object(object_id)
        if obj is None:
            issues.append(problem(fact, field, "所引用的人员或项目尚未在本公司登记。"))
        elif obj["kind"] != kind:
            issues.append(
                problem(
                    fact,
                    field,
                    "薪酬须引用人员，资本化去向须引用项目；账户不能冒充。",
                    code="incompatible_object_kind",
                )
            )
    return issues


def _references(fact):
    if fact["kind"] != "people.remuneration":
        return []
    data, refs = fact["data"], []
    for field in ("employment_id", "contribution_policy_id", "contribution_actual_id"):
        if field in data:
            refs.append(
                (
                    data[field],
                    "result",
                    "policy" if field == "contribution_policy_id" else "calculation",
                )
            )
    tax_basis = data.get("tax_basis", {})
    for locator in (tax_basis.get("pooled_source"), data.get("disbursement_basis")):
        if locator is not None:
            # An official row is a fact, independent of a payroll calculation using it.
            # The tax owner can validate its allocations against remuneration facts.
            refs.append((locator["declaration_id"], "fact", "reference"))
    return list(dict.fromkeys(refs))


def _natural_key(fact):
    data, kind = fact["data"], fact["kind"]
    if kind == "people.remuneration" and data["type"] == "wage":
        return f"people:wage:{data['person_id']}:{fact['period']}"
    if kind == "people.contribution_actual":
        return f"people:contribution_actual:{data['employee_id']}:{fact['period']}"
    if kind == "people.employment":
        return f"people:employment:{data['employee_id']}:{data['effective_from']}"
    return None


def _validate(fact):
    validate_contract("business-facts.schema.json", "Fact", fact)
    if fact["kind"] not in _CALCULATORS:
        raise ContractError("unsupported_business", "用工薪酬模块不能承接其他业务类型。")
    if fact["kind"] == "people.contribution_policy":
        check_policy(fact)
    elif fact["kind"] == "people.contribution_actual":
        check_actual(fact)


class People:
    supported_kinds = tuple(_CALCULATORS)

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        responsibilities, dependencies, issues = [], [], []
        for change in changes:
            business_id = change.get("business_id", change.get("fact", {}).get("business_id"))
            op = change["op"]
            responsibility = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": op,
                "complete_result_slots": [],
                "natural_key": None,
            }
            if op in {"withdraw", "handoff_out"}:
                responsibilities.append(responsibility)
                continue
            fact = change.get("fact") or context.get_fact(business_id)
            if fact is None:
                raise ContractError("invalid_input", "用工薪酬变更缺少完整事实。")
            _validate(fact)
            if fact["business_id"] != business_id:
                raise ContractError("invalid_input", "薪酬责任与事实身份不一致。")
            responsibility.update(
                fact=deepcopy(fact), complete_result_slots=["main"], natural_key=_natural_key(fact)
            )
            responsibilities.append(responsibility)
            issues.extend(_role_issues(fact, context))
            for _, object_id, _ in _roles(fact):
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
            for producer, read_mode, relation_kind in _references(fact):
                dependencies.append(
                    {
                        "consumer_business_id": business_id,
                        "consumer_result_slot": "main",
                        "producer_kind": "business",
                        "producer_id": producer,
                        "producer_public_slot": "main",
                        "read_mode": read_mode,
                        "relation_kind": relation_kind,
                    }
                )
        result = {
            "domain": "people",
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
            raise ContractError("invalid_input", "用工薪酬只接受完整main结果槽。")
        fact = context.get_fact(business_id)
        if fact is None:
            raise ContractError("dependency_conflict", "用工薪酬的当前事实不存在。")
        _validate(fact)
        issues = _role_issues(fact, context)
        if issues:
            raise ContractError(
                "invalid_input"
                if any(item["code"] == "incompatible_object_kind" for item in issues)
                else "needs_information",
                "薪酬引用的人员或项目不符合其业务角色。",
                fact_issues=issues,
            )
        plan = _CALCULATORS[fact["kind"]](fact, context)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        return plan

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "people":
            raise ContractError("invalid_input", "薪酬不能校验其他领域的变更。")
        # Shared ledger validation owns settlement/cost capacities. People has no
        # additional aggregate resource constraint for these four fact kinds.

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "people" or plan["issues"]:
            raise ContractError("invalid_input", "不能保存其他领域或尚有问题的薪酬变更。")
        repo = uow.repository("domains.people")
        for responsibility in plan["businesses"]:
            if responsibility["op"] in {"withdraw", "handoff_out"}:
                repo.remove(responsibility["business_id"])
            else:
                _validate(responsibility["fact"])
                repo.save(responsibility["fact"])

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("domains.people").get(ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if query["company_id"] != read_context.company_id or query["family"] != "people":
            raise ContractError("invalid_input", "薪酬查询的公司或领域不一致。")
        if query["period_from"] > query["period_to"]:
            raise ContractError("invalid_input", "查询起月不能晚于止月。")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("domains.people").query(query)


def create_people() -> People:
    return People()
