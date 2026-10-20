"""One-submission overlays; formal identities remain the ledger's responsibility."""

from collections import defaultdict
from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot

from ._changes import fact_digest


class DomainRegistry:
    def __init__(self, domains):
        self._by_kind = {}
        for domain in domains:
            for kind in domain.supported_kinds:
                if kind in self._by_kind:
                    raise ContractError("invalid_input", f"业务类型重复装配：{kind}")
                self._by_kind[kind] = domain

    def owner(self, kind):
        owner = self._by_kind.get(kind)
        if owner is None:
            raise ContractError("unsupported_business", f"当前运行实例尚未交付：{kind}")
        return owner

    def fact(self, head, read):
        if head is None:
            return None
        facts = self.export_heads([head], read)
        return facts[0] if facts else None

    def export_heads(self, heads, read):
        grouped, result = defaultdict(list), []
        for head in heads:
            grouped[self.owner(head["kind"])].append(head)
        for owner, owned in grouped.items():
            for offset in range(0, len(owned), 500):
                batch = owned[offset : offset + 500]
                facts = owner.export_current([head["business_id"] for head in batch], read)
                indexed = {fact["business_id"]: fact for fact in facts}
                active = {head["business_id"] for head in batch if head["state"] == "active"}
                if set(indexed) != active or len(indexed) != len(facts):
                    raise ContractError("storage_corrupt", "领域导出遗漏或重复了当前业务。")
                for head in batch:
                    if head["state"] == "withdrawn":
                        continue
                    fact = indexed.get(head["business_id"])
                    if fact is None or (
                        fact["kind"] != head["kind"]
                        or fact["period"] != head["period"]
                        or fact_digest(fact) != head["current_digest"]
                    ):
                        raise ContractError("storage_corrupt", "领域正文与当前业务索引不一致。")
                    result.append(deepcopy(fact))
        return result


class SubmissionContext:
    def __init__(self, unit, registry, identity, ledger, records, *, objects=(), facts=None):
        self.read_context = unit
        self._registry, self._identity = registry, identity
        self._ledger, self._records = ledger, records
        self._objects = {obj["object_id"]: obj for obj in objects}
        self._facts = dict(facts or {})
        self._plans = {}
        self._slots = {}
        self._fact_cache = {}

    def add_fact(self, fact):
        self._facts[fact["business_id"]] = deepcopy(fact)

    def declare_slots(self, responsibility):
        self._slots[responsibility["business_id"]] = set(responsibility["complete_result_slots"])

    def add_plan(self, plan):
        self._plans[plan["business_id"], plan["result_slot"]] = plan

    def get_fact(self, business_id):
        if business_id in self._facts:
            return deepcopy(self._facts[business_id])
        if business_id not in self._fact_cache:
            heads = self.read_context.repository("application").heads([business_id])
            self._fact_cache[business_id] = self._registry.fact(
                heads.get(business_id), self.read_context
            )
        return deepcopy(self._fact_cache[business_id])

    def get_object(self, object_id):
        if object_id in self._objects:
            return deepcopy(self._objects[object_id])
        values = self._identity.get_batch([object_id], self.read_context)
        return values[0] if values else None

    def related_facts(self, producer_id, relation_kind):
        raise ContractError(
            "unsupported_business", "按当前依赖及同单事实查询相关业务的能力尚未交付。"
        )

    def get_result(self, business_id, result_slot):
        plan = self._plans.get((business_id, result_slot))
        if plan is not None:
            source = self._ledger.get_result_source(
                business_id, result_slot, self.read_context, plan=plan
            )
            return EvaluatedSlot(source, deepcopy(plan["target"]), deepcopy(plan["basis"]))
        if business_id in self._slots:
            if result_slot not in self._slots[business_id]:
                return None
            raise ContractError("dependency_conflict", "同单所需结果槽尚未完成，须先声明正确依赖。")
        found = next(
            (
                item
                for item in self._ledger.get_results([business_id], self.read_context)
                if item["reference"]["result_slot"] == result_slot
            ),
            None,
        )
        if found is None:
            return None
        return EvaluatedSlot(
            self._ledger.get_result_source(business_id, result_slot, self.read_context),
            found["target"],
            found["basis"],
        )

    def resolve(self, refs, kind):
        if any(ref["business_id"] in self._slots for ref in refs):
            raise ContractError(
                "unsupported_business",
                "同单公开资源解析尚未由账务模块交付；不能以旧余额代替本单结果。",
            )
        return list(self._ledger.resolve(refs, kind, self.read_context))

    def resolve_sources(self, ids):
        return self.require_sources(ids)

    def require_sources(self, ids, *, field="/evidence_ids", business_id=None):
        sources = []
        for offset in range(0, len(ids), 500):
            sources.extend(
                self._records.source_locations(ids[offset : offset + 500], self.read_context)
            )
        mapped = {source["source_id"]: source for source in sources}
        missing = [
            (index, source_id) for index, source_id in enumerate(ids) if source_id not in mapped
        ]
        if missing:
            issues = []
            for offset in range(0, len(missing), 40):
                batch = missing[offset : offset + 40]
                issue = {
                    "code": "source_not_found",
                    "field": f"{field}/{batch[0][0]}" if len(batch) == 1 else field,
                    "meaning": "当前事实或对象档案实际引用的来源身份",
                    "message": "这些来源尚未在本公司登记："
                    + "、".join(source_id for _, source_id in batch),
                    "allowed_precision": ["source"],
                    "reusable_sources": [],
                    "blocking_scope": "business",
                }
                if business_id is not None:
                    issue["business_id"] = business_id
                issues.append(issue)
            raise ContractError(
                "needs_information",
                "所引用来源尚未在本公司登记。",
                fact_issues=issues,
            )
        return [deepcopy(mapped[source_id]) for source_id in ids]
