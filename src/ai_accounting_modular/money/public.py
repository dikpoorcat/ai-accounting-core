"""The only cross-module entry point for real company money."""

from ai_accounting_modular.contracts.public import (
    EvaluationContext,
    ReadContext,
    UnitOfWork,
    validate_contract,
)

from ._common import (
    allocation_refs,
    fail,
    object_roles,
    occurrence_id,
    require_object,
)
from ._payments import (
    check_payroll_unpaid,
    managed_reserve,
    offset,
    payroll_facts,
    payroll_reserve,
    physical,
    settlement,
)
from ._queries import MoneyPage, MoneyQuery, cursor, page_movements, position
from ._reconciliation import (
    groups_from_rows,
    previous_period,
    reconcile_plan,
    statement_for,
    validate_reconciliation,
)
from ._repository import MoneyRepository
from ._statements import check_rows, fund_sources, reserve_internal, statement


def repository_factory():
    from ai_accounting_modular.storage.public import RepositoryFactory, module_schema

    return RepositoryFactory(module_schema("money"), MoneyRepository)


def stable_occurrence_id(business_id: str, public_slot: str) -> str:
    """The same real occurrence retains its identity through open corrections."""
    return occurrence_id(business_id, public_slot)


def _dependency(business_id, producer_id, relation, *, kind="business", mode="result", period=None):
    edge = {
        "consumer_business_id": business_id,
        "consumer_result_slot": "main",
        "producer_kind": kind,
        "producer_id": producer_id,
        "producer_public_slot": "main",
        "read_mode": mode,
        "relation_kind": relation,
    }
    if period is not None:
        edge.update(period_from=period, period_to=period)
    return edge


def _validate_fact(fact):
    validate_contract("business-facts.schema.json", "Fact", fact)
    actual = fact["kind"] == "money.movement" or (
        fact["kind"] == "money.transfer" and fact["data"]["type"] == "physical"
    )
    if actual and fact["effective_on"][:7] != fact["period"]:
        fail("真实资金的实际日期必须属于该业务月份。", "invalid_input")
    if fact["kind"] in {"money.bank_statement", "money.platform_activity"}:
        check_rows(fact)


class Money:
    stable_occurrence_id = staticmethod(stable_occurrence_id)

    supported_kinds = (
        "money.movement",
        "money.transfer",
        "money.bank_statement",
        "money.platform_activity",
        "money.reserve_internal",
        "money.reconciliation",
    )

    def __init__(self, *, ledger=None, identity=None):
        self._ledger, self._identity = ledger, identity

    def describe_change(self, changes, context: EvaluationContext):
        businesses, dependencies = [], []
        for change in changes:
            op = change["op"]
            business_id = change.get("business_id") or change["fact"]["business_id"]
            responsibility = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": op,
                "natural_key": None,
                "complete_result_slots": [],
            }
            if op in {"withdraw", "handoff_out"}:
                businesses.append(responsibility)
                continue
            fact = change.get("fact") or context.get_fact(business_id)
            _validate_fact(fact)
            if fact["business_id"] != business_id or fact["kind"] not in self.supported_kinds:
                fail("资金业务身份或所属种类不一致。", "invalid_input")
            responsibility.update(fact=fact, complete_result_slots=["main"])
            if fact["kind"] in {"money.bank_statement", "money.platform_activity"}:
                account_field = (
                    "funds_account_id"
                    if fact["kind"] == "money.bank_statement"
                    else "platform_account_id"
                )
                responsibility["natural_key"] = fact["data"][account_field] + "/" + fact["period"]
            if fact["kind"] == "money.reconciliation":
                statement = statement_for(fact, context)
                account_id = statement["data"]["funds_account_id"]
                responsibility["natural_key"] = account_id + "/" + fact["period"]
                dependencies.extend(
                    [
                        _dependency(business_id, statement["business_id"], "reference"),
                        _dependency(
                            business_id, account_id, "identity", kind="object", mode="fact"
                        ),
                        _dependency(
                            business_id, account_id, "identity", kind="object", mode="collection"
                        ),
                    ]
                )
                previous = previous_period(fact["period"])
                dependencies[-1].update(
                    period_from=previous or fact["period"], period_to=fact["period"]
                )
                if previous is not None:
                    for prior in context.facts_in_scope(
                        ("money.reconciliation",), previous, previous
                    ):
                        old_statement = statement_for(prior, context)
                        if old_statement["data"]["funds_account_id"] == account_id:
                            dependencies.append(
                                _dependency(business_id, prior["business_id"], "calculation")
                            )
                for opening in context.facts_in_scope(
                    ("opening.package",), fact["period"], fact["period"]
                ):
                    dependencies.append(
                        _dependency(business_id, opening["business_id"], "calculation")
                    )
            if fact["kind"] == "money.movement" and fact["data"]["type"] == "payroll_reserve":
                dependencies.append(
                    _dependency(
                        business_id,
                        "people.remuneration",
                        "settlement",
                        kind="scope",
                        mode="collection",
                        period=fact["data"]["payroll_period"],
                    )
                )
                for wage in payroll_facts(fact, context):
                    dependencies.append(
                        _dependency(business_id, wage["business_id"], "calculation")
                    )
            businesses.append(responsibility)
            for object_id, kinds, field in object_roles(fact):
                require_object(fact, context, object_id, kinds, field)
                edge = _dependency(business_id, object_id, "identity", kind="object", mode="fact")
                if edge not in dependencies:
                    dependencies.append(edge)
                if (
                    "platform_account" in kinds
                    and context.get_object(object_id)["kind"] == "platform_account"
                ):
                    collection = _dependency(
                        business_id,
                        object_id,
                        "identity",
                        kind="object",
                        mode="collection",
                        period=fact["period"],
                    )
                    if collection not in dependencies:
                        dependencies.append(collection)
            for allocation in allocation_refs(fact):
                producer = allocation["source"]["business_id"]
                for mode in ("result", "collection"):
                    edge = _dependency(business_id, producer, "settlement", mode=mode)
                    if edge not in dependencies:
                        dependencies.append(edge)
        result = {
            "domain": "money",
            "businesses": businesses,
            "dependencies": dependencies,
            "issues": [],
        }
        validate_contract("business-facts.schema.json", "ChangeDescription", result)
        return result

    def evaluate_slot(self, business_id, result_slot, context: EvaluationContext):
        if result_slot != "main":
            fail("资金业务只支持完整main结果槽。", "invalid_input")
        fact = context.get_fact(business_id)
        if fact is None:
            fail("当前资金事实不存在。")
        _validate_fact(fact)
        for object_id, kinds, field in object_roles(fact):
            require_object(fact, context, object_id, kinds, field)
        kind, data = fact["kind"], fact["data"]
        if kind == "money.movement":
            calculators = {
                "settlement": settlement,
                "managed_reserve": managed_reserve,
                "payroll_reserve": payroll_reserve,
            }
            calculator = calculators.get(data["type"])
            if calculator is None:
                fail("该资金组合的完整检查尚未实现。", "unsupported_business")
            plan = calculator(fact, context)
        elif kind == "money.transfer":
            plan = (physical if data["type"] == "physical" else offset)(fact, context)
        elif kind in {"money.bank_statement", "money.platform_activity"}:
            plan = statement(fact, context)
        elif kind == "money.reserve_internal":
            plan = reserve_internal(fact, context)
        elif kind == "money.reconciliation":
            plan = reconcile_plan(fact, context)
        else:
            fail("尚未实现此资金业务。", "unsupported_business")
        if plan["fund_movements"]:
            for location in self.fund_sources(fact, plan["fund_movements"], context):
                if location not in plan["basis"]["source_locations"]:
                    plan["basis"]["source_locations"].append(location)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        return plan

    def validate_candidate(self, change_plan, context: EvaluationContext) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", change_plan)
        if change_plan["domain"] != "money" or change_plan["issues"]:
            fail("资金候选计划不属于本模块或仍有未解问题。", "invalid_input")
        # All final global resource capacities are checked by ledger. This
        # owner checks raw-platform exclusivity against the complete fact set.
        for plan in change_plan["plans"]:
            fact = context.get_fact(plan["business_id"])
            if fact["kind"] == "money.movement" and fact["data"]["type"] == "payroll_reserve":
                check_payroll_unpaid(fact, context)
            if fact["kind"] == "money.reconciliation":
                validate_reconciliation(fact, context)
        self.validate_occurrences(change_plan["plans"], context)

    def fund_sources(self, fact, fund_movements, context: EvaluationContext):
        return fund_sources(fact, fund_movements, context)

    def validate_occurrences(self, plans, context: EvaluationContext) -> None:
        """Check every owner's real money against one complete candidate."""
        consumers = {}
        for plan in plans:
            validate_contract("business-facts.schema.json", "PostingPlan", plan)
            fact = context.get_fact(plan["business_id"])
            if fact is None:
                fail("实际资金计划缺少当前完整业务事实。")
            for row_id in fact["data"].get("platform_row_ids", ()):
                previous = consumers.setdefault(row_id, fact["business_id"])
                if previous != fact["business_id"]:
                    fail("同一平台原行不能同时承接多个业务的真实资金或代管内部移动。")
            if plan["fund_movements"]:
                self.fund_sources(fact, plan["fund_movements"], context)

    def apply_current(self, plan, uow: UnitOfWork) -> None:
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "money" or plan["issues"]:
            fail("不能保存其他领域或仍有核算问题的资金计划。", "invalid_input")
        repo = uow.repository("money")
        current = {item["business_id"]: item.get("fact") for item in plan["businesses"]}
        main_objects = {}
        for business_id, fact in current.items():
            if fact is not None and fact["kind"] == "money.reconciliation":
                statement_id = fact["data"]["statement_id"]
                statement = current.get(statement_id) or repo.get([statement_id])[0]
                main_objects[business_id] = statement["data"]["funds_account_id"]
        repo.replace_current(current, main_objects)
        desired_groups = {}
        for responsibility in plan["businesses"]:
            fact = responsibility.get("fact")
            if fact is not None and fact["kind"] == "money.reconciliation":
                statement = repo.get([fact["data"]["statement_id"]])[0]
                desired_groups[fact["business_id"]] = groups_from_rows(fact, statement)
        repo.replace_groups(desired_groups)

    def register_occurrences(self, plans, uow: UnitOfWork) -> None:
        """Register stable identities from all domains before ledger adoption."""
        registrations = {}
        repo = uow.repository("money")
        for plan in plans:
            validate_contract("business-facts.schema.json", "PostingPlan", plan)
            for actual in plan["fund_movements"]:
                key = plan["business_id"], actual["public_slot"]
                if key in registrations or actual["movement_id"] != occurrence_id(*key):
                    fail(
                        "正式实际资金身份必须唯一并由业务身份和公开资源名稳定生成。",
                        "invalid_input",
                    )
                registrations[key] = actual["movement_id"]
        business_ids = list(dict.fromkeys(key[0] for key in registrations))
        existing = []
        for batch_start in range(0, len(business_ids), 500):
            existing.extend(
                repo.occurrences(business_ids=business_ids[batch_start : batch_start + 500])
            )
        identities = {movement_id: key for key, movement_id in registrations.items()}
        if len(identities) != len(registrations):
            fail("不同业务资源不能使用相同的真实资金身份。", "invalid_input")
        movement_ids = list(identities)
        for batch_start in range(0, len(movement_ids), 500):
            existing.extend(repo.occurrences(ids=movement_ids[batch_start : batch_start + 500]))
        for row in existing:
            key = row["business_id"], row["public_slot"]
            if (key in registrations and registrations[key] != row["occurrence_id"]) or (
                row["occurrence_id"] in identities and identities[row["occurrence_id"]] != key
            ):
                fail("当前真实资金登记与稳定发生身份不一致。", "storage_corrupt")
        registered = {row["occurrence_id"] for row in existing}
        # Every semantic check precedes insertion; a later SQL failure poisons
        # the shared storage unit and cannot leave a partial committed registry.
        for key, movement_id in registrations.items():
            if movement_id not in registered:
                repo.register_occurrence(*key, movement_id)

    def export_current(self, ids, read_context: ReadContext):
        return read_context.repository("money").get(ids)

    def query_current(self, query, read_context: ReadContext):
        validate_contract("core.schema.json", "BusinessQuery", query)
        if query["company_id"] != read_context.company_id or query["family"] != "money":
            fail("资金查询公司或领域不一致。", "invalid_input")
        if query["period_from"] > query["period_to"]:
            fail("查询起月不能晚于止月。", "invalid_input")
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("money").query(query)

    def query_accounts(self, query, read_context: ReadContext):
        if self._identity is None:
            fail("资金账户查询未装配身份读取端口。", "unsupported_business")
        if query.get("kind") not in {"bank_account", "cash_account", "platform_account"}:
            fail("资金账户查询须选择真实公司账户类型。", "invalid_input")
        return self._identity.find(query, read_context)

    def reconciliation(self, query, read_context: ReadContext):
        if self._ledger is None:
            fail("银行核对读取未装配正式账务端口。", "unsupported_business")
        if query.get("kind", "money.reconciliation") != "money.reconciliation":
            fail("银行核对查询只能选择核对业务。", "invalid_input")
        facts = self.query_current({**query, "kind": "money.reconciliation"}, read_context)
        results = self._ledger.get_results([fact["business_id"] for fact in facts], read_context)
        by_id = {result["reference"]["business_id"]: result for result in results}
        if any(fact["business_id"] not in by_id for fact in facts):
            fail("当前银行核对缺少正式采用结果。", "storage_corrupt")
        return tuple({"fact": fact, "result": by_id[fact["business_id"]]} for fact in facts)

    def query_movements(self, query: MoneyQuery, read_context: ReadContext) -> MoneyPage:
        if self._ledger is None:
            fail("真实资金查询未装配正式账务读取端口。", "unsupported_business")
        # Validate the query before it can initiate a resource-scope read.
        position(query, read_context, "movements")
        definitions = self._ledger.fund_movements(
            query.account_id, query.period_from, query.period_to, read_context
        )
        return page_movements(query, read_context, definitions)

    def query_statement_rows(self, query: MoneyQuery, read_context: ReadContext) -> MoneyPage:
        after, signature = position(query, read_context, "statement_rows")
        if after is not None and (len(after) != 2 or not all(type(item) is str for item in after)):
            fail("原行分页位置无效。", "invalid_input")
        count, rows = read_context.repository("money").statement_page(
            query.account_id, query.period_from, query.period_to, after, query.limit
        )
        has_more = len(rows) > query.limit
        rows = rows[: query.limit]
        return MoneyPage(
            tuple(rows),
            count,
            read_context.head,
            cursor(signature, [rows[-1]["actual_on"], rows[-1]["row_id"]]) if has_more else None,
        )


def create_money(*, ledger=None, identity=None) -> Money:
    return Money(ledger=ledger, identity=identity)
