"""The tax domain's only public entry point: sourced facts, never free entries."""

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

from ._actual import actual, declarations
from ._common import interval, reject
from ._credits import credit
from ._income_tax import (
    assessments,
    carry_candidates,
    check_chronology,
    income_tax,
    order,
    prepayment,
    previous,
)
from ._netting import SLOT as NETTING_SLOT
from ._netting import dependencies as netting_dependencies
from ._netting import netting
from ._policies import POLICIES, check_policy, policy
from ._receipt_basis import receipts
from ._repository import TaxesRepository
from ._tax_point import tax_point
from ._vat import vat

_CALCULATORS = {
    **dict.fromkeys(POLICIES, policy),
    "taxes.prepayment_basis": prepayment,
    "taxes.tax_point": tax_point,
    "taxes.actual_declaration": actual,
    "taxes.tax_credit": credit,
    "taxes.assessment": None,
}


def tax_point_periods(fact: Fact, context: EvaluationContext) -> tuple[str, ...]:
    """Declare tax-release months from the same facts used by tax-point evaluation."""
    if fact["kind"] != "taxes.tax_point":
        reject("详细税点必须引用税点推导业务。", "invalid_input")
    data = fact["data"]
    if data.get("type") == "owner_receipt_default":
        return tuple(
            sorted({row["period"] for row, _ in receipts(data["target_business_id"], context)})
        )
    return tuple(sorted({event["month"] for event in data["events"]}))


def repository_factory() -> RepositoryFactory:
    return RepositoryFactory(module_schema("domains.taxes"), TaxesRepository)


def _edge(
    fact,
    producer,
    *,
    mode="result",
    relation="calculation",
    producer_kind="business",
    start=None,
    end=None,
):
    row = {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": "main",
        "producer_kind": producer_kind,
        "producer_id": producer,
        "producer_public_slot": "main",
        "read_mode": mode,
        "relation_kind": relation,
    }
    if start is not None:
        row["period_from"] = start
    if end is not None:
        row["period_to"] = end
    return row


def _collection(fact, producer, relation="reference"):
    return _edge(
        fact, producer, mode="collection", relation=relation, start="0001-01", end="9999-12"
    )


def _aggregate(fact, collection, start, end):
    return _edge(fact, collection, producer_kind="aggregate", start=start, end=end)


def _scope(fact, kind, start="0001-01", end="9999-12"):
    return _edge(fact, kind, mode="collection", producer_kind="scope", start=start, end=end)


def _direct_dependencies(fact):
    data, kind = fact["data"], fact["kind"]
    edges = []
    if kind == "taxes.tax_point":
        edges.append(_edge(fact, data["target_business_id"], mode="fact", relation="reference"))
        for event in data.get("events", ()):
            if event["type"] == "receipt":
                edges.append(
                    _edge(
                        fact,
                        event["receipt_source"]["business_id"],
                        mode="fact",
                        relation="reference",
                    )
                )
    elif kind == "taxes.actual_declaration":
        if data["type"] == "payroll":
            for row in data["rows"]:
                edges.append(
                    _edge(
                        fact,
                        row["person_id"],
                        mode="fact",
                        relation="identity",
                        producer_kind="object",
                    )
                )
                edges.extend(
                    _edge(fact, item["remuneration_id"], mode="fact", relation="reference")
                    for item in row["allocations"]
                )
        else:
            edges.append(_edge(fact, data["assessment_id"], mode="fact", relation="reference"))
    elif kind == "taxes.tax_credit":
        edges.append(_edge(fact, data["original_assessment_id"], relation="reference"))
        for row in data["returns"]:
            edges.append(_edge(fact, row["return_id"], relation="reference"))
            edges.append(
                _edge(fact, row["original_business_id"], mode="fact", relation="reference")
            )
    elif kind == "taxes.assessment" and data["type"] == "vat":
        edges.extend(
            _edge(fact, identity, relation="reference") for identity in data.get("credit_ids", ())
        )
    return edges


def _fact_relations(fact):
    relations = [
        {
            key: edge[key]
            for key in ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
        }
        for edge in _direct_dependencies(fact)
    ]
    if fact["kind"] == "taxes.assessment":
        for field in ("vat_policy_id", "surtax_policy_id", "policy_id", "prepayment_basis_id"):
            if field in fact["data"]:
                relations.append(
                    {
                        "consumer_business_id": fact["business_id"],
                        "producer_kind": "business",
                        "producer_id": fact["data"][field],
                        "relation_kind": "calculation"
                        if field == "prepayment_basis_id"
                        else "policy",
                    }
                )
    return relations


def _dependencies(fact, context, actuals=()):
    data, kind = fact["data"], fact["kind"]
    edges = _direct_dependencies(fact)
    if kind == "taxes.tax_point" and data.get("type") == "owner_receipt_default":
        edges.append(_collection(fact, data["target_business_id"], relation="settlement"))
        edges.extend(
            _edge(fact, receipt["business_id"], mode="fact", relation="reference")
            for receipt, _ in receipts(data["target_business_id"], context)
        )
    elif kind == "taxes.tax_credit":
        periods = set()
        edges.append(_collection(fact, data["original_assessment_id"]))
        for row in data["returns"]:
            edges.append(_collection(fact, row["return_id"]))
            returned = context.get_fact(row["return_id"])
            if returned is not None:
                periods.add(returned["period"])
        edges.extend(_aggregate(fact, "tax_sales", month, month) for month in sorted(periods))
    elif kind == "taxes.assessment":
        source = declarations(fact, context, actuals)
        edges.append(_collection(fact, fact["business_id"]))
        if source is not None:
            edges.append(_edge(fact, source["business_id"], relation="reference"))
        if data["type"] == "vat":
            edges.append(_aggregate(fact, "tax_sales", data["start_month"], data["end_month"]))
            for field in ("vat_policy_id", "surtax_policy_id"):
                if field in data and (source is None or context.get_fact(data[field]) is not None):
                    edges.append(_edge(fact, data[field], relation="policy"))
            for identity in data.get("credit_ids", ()):
                edges.append(_collection(fact, identity))
            edges.append(_scope(fact, "taxes.assessment"))
        else:
            edges.extend(netting_dependencies(fact, context))
            year = f"{data['tax_year']:04d}"
            edges.append(_scope(fact, "taxes.assessment", year + "-01", "9999-12"))
            before = previous(fact, context)
            if before is not None:
                edges.append(_edge(fact, before["business_id"]))
            else:
                for kind in ("opening.package", "reports.continuation"):
                    edges.append(
                        _scope(
                            fact,
                            kind,
                            context.read_context.start_period,
                            context.read_context.start_period,
                        )
                    )
                for row in carry_candidates(fact, context):
                    edges.append(_edge(fact, row["business_id"]))
            for earlier in assessments(fact, context):
                if order(earlier) < order(fact):
                    edges.append(_collection(fact, earlier["business_id"], relation="settlement"))
            if data["method"] == "computed_provision" and source is None:
                edges.extend(
                    (
                        _edge(fact, data["policy_id"], relation="policy"),
                        _edge(fact, data["prepayment_basis_id"]),
                        _aggregate(fact, "pre_tax", year + "-01", data["quarter_end"]),
                    )
                )
    return edges


def _natural_key(fact):
    data, kind = fact["data"], fact["kind"]
    if kind == "taxes.tax_point":
        return "taxes:point:" + data["target_business_id"]
    if kind == "taxes.prepayment_basis":
        return f"taxes:prepayment:{data['tax_year']}:{data['quarter_end']}"
    if kind == "taxes.actual_declaration" and data["type"] != "payroll":
        return f"taxes:actual:{data['type']}:{data['assessment_id']}"
    if kind == "taxes.assessment" and data["type"] == "income_tax":
        end, annual = order(fact)
        return f"taxes:income:{data['tax_year']}:{end}:{'annual' if annual else 'provision'}"
    return None


def _validate(fact):
    validate_contract("business-facts.schema.json", "Fact", fact)
    if fact["kind"] not in _CALCULATORS:
        reject("税费模块不能承接其他种类业务。", "unsupported_business")
    if fact["kind"] in POLICIES:
        check_policy(fact)


class Taxes:
    supported_kinds = tuple(_CALCULATORS)

    def declare_relations(self, changes, context: EvaluationContext) -> tuple[DirectRelation, ...]:
        return tuple(
            relation
            for change in changes
            if change["op"] not in {"withdraw", "handoff_out"}
            for relation in _fact_relations(
                change.get("fact") or context.get_fact(change["business_id"])
            )
        )

    def describe_change(self, changes, context: EvaluationContext) -> ChangeDescription:
        businesses, dependencies, publications = [], [], []
        actuals = tuple(
            change["fact"]
            for change in changes
            if change.get("fact", {}).get("kind") == "taxes.actual_declaration"
        )
        for change in changes:
            business_id = change.get("business_id", change.get("fact", {}).get("business_id"))
            row = {
                "business_id": business_id,
                "expected_seq": change["expected_seq"],
                "op": change["op"],
                "complete_result_slots": [],
                "natural_key": None,
            }
            businesses.append(row)
            if change["op"] in ("withdraw", "handoff_out"):
                continue
            fact = change.get("fact") or context.get_fact(business_id)
            if fact is None:
                reject("税费变更缺少完整当前事实。", "invalid_input")
            _validate(fact)
            if fact["business_id"] != business_id:
                reject("税费责任必须与本次事实身份一致。", "invalid_input")
            row.update(
                fact=deepcopy(fact), complete_result_slots=["main"], natural_key=_natural_key(fact)
            )
            if fact["kind"] == "taxes.assessment" and fact["data"]["type"] == "income_tax":
                row["complete_result_slots"].append(NETTING_SLOT)
            dependencies.extend(_dependencies(fact, context, actuals))
            if fact["kind"] == "taxes.assessment" and fact["data"]["type"] == "vat":
                publications.append(
                    {
                        "business_id": business_id,
                        "result_slot": "main",
                        "collection": "pre_tax",
                        "period_from": fact["period"],
                        "period_to": fact["period"],
                    }
                )
        result = {
            "domain": "taxes",
            "businesses": businesses,
            "dependencies": dependencies,
            "publications": publications,
            "issues": [],
        }
        validate_contract("business-facts.schema.json", "ChangeDescription", result)
        return result

    def evaluate_slot(self, business_id, result_slot, context: EvaluationContext) -> PostingPlan:
        fact = context.get_fact(business_id)
        if fact is None:
            reject("税费当前事实不存在。")
        _validate(fact)
        if result_slot == NETTING_SLOT:
            if fact["kind"] != "taxes.assessment" or fact["data"]["type"] != "income_tax":
                reject("内部所得税抵减只能属于所得税累计节点。", "invalid_input")
            plan = netting(fact, context)
            validate_contract("business-facts.schema.json", "PostingPlan", plan)
            return plan
        if result_slot != "main":
            reject("税费结果槽不属于本业务。", "invalid_input")
        calculator = _CALCULATORS[fact["kind"]]
        if fact["kind"] == "taxes.assessment":
            calculator = vat if fact["data"]["type"] == "vat" else income_tax
        plan = calculator(fact, context)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        return plan

    def validate_candidate(self, plan: ChangePlan, context: EvaluationContext):
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "taxes" or plan["issues"]:
            reject("税费不能核验其他领域或仍有问题的候选。", "invalid_input")
        if not any(
            row.get("fact", {}).get("kind") == "taxes.assessment" for row in plan["businesses"]
        ):
            return
        assessments = context.facts_in_scope(("taxes.assessment",), "0001-01", "9999-12")
        vat_periods, income_keys, income_facts = [], set(), []
        for fact in assessments:
            data = fact["data"]
            if data["type"] == "vat":
                interval(data["start_month"], data["end_month"], same_year=True)
                vat_periods.append((data["start_month"], data["end_month"]))
            else:
                income_facts.append(fact)
                key = data["tax_year"], *order(fact)
                if key in income_keys:
                    reject("同一税年及累计边界不能重复计提所得税。")
                income_keys.add(key)
        check_chronology(income_facts)
        vat_periods.sort()
        if any(
            right[0] <= left[1] for left, right in zip(vat_periods, vat_periods[1:], strict=False)
        ):
            reject("同一税月不能由两个当前VAT计提重复覆盖。")

    def apply_current(self, plan: ChangePlan, uow: UnitOfWork):
        validate_contract("business-facts.schema.json", "ChangePlan", plan)
        if plan["domain"] != "taxes" or plan["issues"]:
            reject("不能保存其他领域或尚有问题的税费变更。", "invalid_input")
        repo = uow.repository("domains.taxes")
        for row in plan["businesses"]:
            if row["op"] in ("withdraw", "handoff_out"):
                repo.remove(row["business_id"])
            else:
                _validate(row["fact"])
                repo.save(row["fact"])

    def export_current(self, ids, read_context: ReadContext) -> tuple[Fact, ...]:
        return read_context.repository("domains.taxes").get(ids)

    def query_current(self, query: BusinessQuery, read_context: ReadContext) -> tuple[Fact, ...]:
        validate_contract("core.schema.json", "BusinessQuery", query)
        if query["company_id"] != read_context.company_id or query["family"] != "taxes":
            reject("税费查询的公司或领域不一致。", "invalid_input")
        interval(query["period_from"], query["period_to"])
        if query.get("status") == "withdrawn":
            return ()
        return read_context.repository("domains.taxes").query(query)


def create_taxes() -> Taxes:
    return Taxes()
