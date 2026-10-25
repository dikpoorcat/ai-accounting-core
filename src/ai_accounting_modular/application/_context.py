"""One-submission overlays; formal identities remain the ledger's responsibility."""

from collections import defaultdict
from collections.abc import Sequence
from contextlib import contextmanager
from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    DomainOwner,
    EvaluatedSlot,
    ResourceCutoff,
    validate_contract,
)

from ._changes import fact_digest
from ._resources import check_scope as check_resource_scope


class DomainRegistry:
    def __init__(self, domains: Sequence[DomainOwner]):
        self._by_kind = {}
        for domain in domains:
            if not callable(getattr(domain, "declare_relations", None)):
                raise ContractError(
                    "invalid_input",
                    f"领域装配缺少直接关系声明端口declare_relations：{type(domain).__name__}。",
                )
            for kind in domain.supported_kinds:
                if kind in self._by_kind:
                    raise ContractError("invalid_input", f"业务类型重复装配：{kind}")
                self._by_kind[kind] = domain

    @property
    def supported_kinds(self):
        return tuple(sorted(self._by_kind))

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
    def __init__(
        self,
        unit,
        registry,
        identity,
        ledger,
        records,
        *,
        objects=(),
        facts=None,
        money=None,
        evaluation_period_to,
        previous_period_to=None,
    ):
        self.read_context = unit
        self._registry, self._identity = registry, identity
        self._ledger, self._records = ledger, records
        self._money = money
        self.evaluation_period_to = evaluation_period_to
        self.previous_period_to = previous_period_to or evaluation_period_to
        self._objects = {obj["object_id"]: obj for obj in objects}
        self._facts = dict(facts or {})
        self._plans = {}
        self._slots = {}
        self._fact_cache = {}
        self._result_cache = {}
        self._stored_results = {}
        self._dependencies = ()
        self._relations = ()
        self._relation_roots = frozenset()
        self._candidate = None
        self._candidate_complete = False
        self._active_slots = frozenset()
        self.pending_slots = frozenset()
        self._retained_results = {}
        self.previous_result_refs = ()

    def current_result_refs(self, business_ids):
        return self._ledger.get_result_refs(business_ids, self.read_context)

    def select_results(self, pending_slots, references, complete_slots):
        self.pending_slots = frozenset(pending_slots)
        self.previous_result_refs = tuple(references[key] for key in sorted(references))
        self._retained_results = {
            key: references[key] for key in sorted(complete_slots - self.pending_slots)
        }

    def retained_results(self, business_ids):
        ids = set(business_ids)
        return [ref for key, ref in self._retained_results.items() if key[0] in ids]

    def set_relations(self, business_ids, relations):
        """Replace the complete direct directory before any final description."""
        self._relation_roots = frozenset(business_ids)
        self._relations = tuple(relations)

    @property
    def declared_relations(self):
        return self._relations

    def reset_descriptions(self):
        self._slots.clear()

    def begin_candidate(self, descriptions, correction_period):
        self._candidate_complete = False
        self._result_cache.clear()
        self._dependencies = tuple(
            edge for _, description in descriptions for edge in description["dependencies"]
        )
        self._candidate = self._ledger.begin_candidate(
            [business for _, description in descriptions for business in description["businesses"]],
            self.read_context,
            correction_period=correction_period,
            publications=[
                item for _, description in descriptions for item in description["publications"]
            ],
            resource_uses=[edge for edge in self._dependencies if edge["read_mode"] == "use"],
            retained_results=list(self._retained_results.values()),
        )

    @contextmanager
    def result_scope(self, slots):
        previous = self._active_slots
        self._active_slots = frozenset(slots)
        try:
            yield
        finally:
            self._active_slots = previous

    def _check_aggregate_scope(self, collection, period_from, period_to):
        self._periods(period_from, period_to)
        if period_from is None or period_to is None:
            raise ContractError("invalid_input", "核算集合读取必须给出完整月份范围。")
        if not any(
            (edge["consumer_business_id"], edge["consumer_result_slot"]) in self._active_slots
            and edge["producer_kind"] == "aggregate"
            and edge["producer_id"] == collection
            and edge["period_from"] <= period_from <= period_to <= edge["period_to"]
            for edge in self._dependencies
        ):
            raise ContractError("dependency_conflict", "当前结果槽未声明覆盖本次集合读取的依赖。")

    def tax_sales(self, period_from, period_to):
        self._check_aggregate_scope("tax_sales", period_from, period_to)
        return self._candidate.tax_sales(period_from, period_to)

    def pre_tax(self, period_from, period_to):
        self._check_aggregate_scope("pre_tax", period_from, period_to)
        return self._candidate.pre_tax(period_from, period_to)

    def complete_candidate(self):
        self._candidate.complete()
        self._candidate_complete = True

    def add_fact(self, fact):
        self._facts[fact["business_id"]] = deepcopy(fact)

    def declare_slots(self, responsibility):
        if responsibility["op"] == "handoff_out":
            return
        if responsibility["business_id"] in self._slots:
            raise ContractError("invalid_input", "同一业务不能重复声明有效采用责任。")
        self._slots[responsibility["business_id"]] = set(responsibility["complete_result_slots"])

    def add_plan(self, plan):
        self._candidate.stage(plan)
        key = plan["business_id"], plan["result_slot"]
        self._plans[key] = plan
        self._result_cache.pop(key, None)

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

    def _current_facts(self, ids):
        ids = sorted(set(ids))
        missing = [key for key in ids if key not in self._facts and key not in self._fact_cache]
        repo = self.read_context.repository("application")
        for offset in range(0, len(missing), 500):
            batch = missing[offset : offset + 500]
            heads = repo.heads(batch)
            loaded = {
                fact["business_id"]: fact
                for fact in self._registry.export_heads(list(heads.values()), self.read_context)
            }
            self._fact_cache.update({key: loaded.get(key) for key in batch})
        return tuple(
            deepcopy(value)
            for key in ids
            if (value := self._facts.get(key) if key in self._facts else self._fact_cache.get(key))
            is not None
        )

    @staticmethod
    def _periods(period_from, period_to):
        for period in (period_from, period_to):
            if period is not None:
                validate_contract("core.schema.json", "Month", period)
        if period_from and period_to and period_from > period_to:
            raise ContractError("invalid_input", "事实范围起月不能晚于末月。")

    def related_facts(
        self,
        producer_id,
        relation_kind,
        *,
        producer_kind="business",
        period_from=None,
        period_to=None,
    ):
        return self.related_facts_many(
            (producer_id,),
            relation_kind,
            producer_kind=producer_kind,
            period_from=period_from,
            period_to=period_to,
        )

    def related_facts_many(
        self,
        producer_ids,
        relation_kind,
        *,
        producer_kind="business",
        period_from=None,
        period_to=None,
    ):
        if len(producer_ids) > 500:
            raise ContractError("invalid_input", "一次最多查询500个关联来源身份。")
        for producer_id in producer_ids:
            validate_contract("core.schema.json", "Id", producer_id)
        producer_ids = set(producer_ids)
        self._periods(period_from, period_to)
        if producer_kind not in ("business", "object"):
            raise ContractError("invalid_input", "相关事实须指明业务或对象关系。")
        repo = self.read_context.repository("application")
        ids = (
            set(
                repo.related_ids(
                    producer_kind,
                    producer_ids,
                    relation_kind,
                    period_from=period_from,
                    period_to=period_to,
                )
            )
            - self._relation_roots
        )
        ids.update(
            edge["consumer_business_id"]
            for edge in self._relations
            if edge["producer_kind"] == producer_kind
            and edge["producer_id"] in producer_ids
            and edge["relation_kind"] == relation_kind
        )
        return tuple(
            fact
            for fact in self._current_facts(ids)
            if (period_from is None or fact["period"] >= period_from)
            and (period_to is None or fact["period"] <= period_to)
        )

    def facts_in_scope(self, kinds, period_from, period_to):
        self._periods(period_from, period_to)
        if not kinds or period_from is None or period_to is None:
            raise ContractError("invalid_input", "事实集合查询须给出种类和完整月份范围。")
        for kind in kinds:
            validate_contract("business-facts.schema.json", "FactKind", kind)
        heads = self.read_context.repository("application").scope_heads(
            kinds, period_from, period_to
        )
        ids = {head["business_id"] for head in heads} | {
            key
            for key, fact in self._facts.items()
            if fact is not None
            and fact["kind"] in kinds
            and period_from <= fact["period"] <= period_to
        }
        return tuple(
            fact
            for fact in self._current_facts(ids)
            if fact["kind"] in kinds and period_from <= fact["period"] <= period_to
        )

    def resource_definitions(self, refs, kind):
        return self._candidate.resource_definitions(refs, kind)

    def _check_resource_read(self, refs, kind, cutoff, single, multiple, components):
        if not self._candidate_complete:
            if not isinstance(cutoff, ResourceCutoff):
                raise ContractError("invalid_input", "资源读取须提供明确截止月份及可选实际日。")
            if any(
                not isinstance(item, tuple) or len(item) != 2
                for item in ([single] if single is not None else []) + list(multiple)
            ):
                raise ContractError("invalid_input", "排除范围须由明确业务及结果槽组成。")
            excluded = frozenset(([single] if single is not None else []) + list(multiple))
            check_resource_scope(self, refs, kind, cutoff, excluded, components)
            for definition in self._candidate.resource_definitions(refs, kind):
                if not any(
                    edge["read_mode"] == "result"
                    and edge["producer_kind"] == "business"
                    and edge["producer_id"] == definition.source["business_id"]
                    and edge["producer_public_slot"] == definition.source["result_slot"]
                    and (edge["consumer_business_id"], edge["consumer_result_slot"])
                    in self._active_slots
                    for edge in self._dependencies
                ):
                    raise ContractError(
                        "dependency_conflict", "余额依赖未声明实际生产该资源的结果槽。"
                    )

    def resource_states(
        self,
        refs,
        kind,
        cutoff,
        *,
        excluding_consumer=None,
        excluding_consumers=(),
        components=None,
    ):
        self._check_resource_read(
            refs, kind, cutoff, excluding_consumer, excluding_consumers, components
        )
        return self._candidate.resource_states(
            refs,
            kind,
            cutoff,
            excluding_consumer=excluding_consumer,
            excluding_consumers=excluding_consumers,
            components=components,
        )

    def resource_windows(
        self,
        refs,
        kind,
        cutoff,
        *,
        excluding_consumer=None,
        excluding_consumers=(),
        components=None,
    ):
        self._check_resource_read(
            refs, kind, cutoff, excluding_consumer, excluding_consumers, components
        )
        return self._candidate.resource_windows(
            refs,
            kind,
            cutoff,
            excluding_consumer=excluding_consumer,
            excluding_consumers=excluding_consumers,
            components=components,
        )

    def fund_movements(self, account_id, period_from, period_to):
        return self._candidate.fund_movements(account_id, period_from, period_to)

    def movement_id(self, business_id, public_slot):
        if self._money is None:
            raise ContractError("invalid_operation", "实际资金身份模块尚未装配。")
        return self._money.stable_occurrence_id(business_id, public_slot)

    def actual_settled_fen(self, source, cutoff):
        if self._money is None:
            raise ContractError("invalid_operation", "实际资金模块尚未装配，不能核定真实清偿。")
        return self._money.actual_settled_fen(source, cutoff, self)

    def fund_sources(self, fact, fund_movements):
        if self._money is None:
            raise ContractError("invalid_operation", "实际资金来源模块尚未装配。")
        return self._money.fund_sources(fact, fund_movements, self)

    def validate_occurrences(self, plans):
        if not any(plan["fund_movements"] for plan in plans):
            return
        if self._money is None:
            raise ContractError("invalid_operation", "实际资金来源模块尚未装配。")
        self._money.validate_occurrences(plans, self)

    def get_result(self, business_id, result_slot):
        key = business_id, result_slot
        if key not in self._result_cache:
            self._result_cache[key] = self._load_result(business_id, result_slot)
        return deepcopy(self._result_cache[key])

    def _load_result(self, business_id, result_slot):
        plan = self._plans.get((business_id, result_slot))
        if plan is not None:
            source = self._ledger.get_result_source(
                business_id, result_slot, self.read_context, plan=plan
            )
            return EvaluatedSlot(source, deepcopy(plan["target"]), deepcopy(plan["basis"]))
        if business_id in self._slots:
            if result_slot not in self._slots[business_id]:
                return None
            retained = self._retained_results.get((business_id, result_slot))
            if retained is None:
                raise ContractError(
                    "dependency_conflict", "同单所需结果槽尚未完成，须先声明正确依赖。"
                )
            found = self._ledger.get_result(
                retained["result_id"], retained["generation"], self.read_context
            )
            return EvaluatedSlot(
                self._ledger.get_result_source(business_id, result_slot, self.read_context),
                found["target"],
                found["basis"],
            )
        if business_id not in self._stored_results:
            self._stored_results[business_id] = {
                item["reference"]["result_slot"]: item
                for item in self._ledger.get_results([business_id], self.read_context)
            }
        found = self._stored_results[business_id].get(result_slot)
        if found is None:
            return None
        return EvaluatedSlot(
            self._ledger.get_result_source(business_id, result_slot, self.read_context),
            found["target"],
            found["basis"],
        )

    def resolve_sources(self, ids):
        return self.require_sources(ids)

    def require_sources(self, ids, *, field="/evidence_ids", business_id=None, source_paths=None):
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
                    "field": (
                        field + source_paths[batch[0][1]][0]
                        if source_paths is not None and len(batch) == 1
                        else f"{field}/{batch[0][0]}"
                        if len(batch) == 1
                        else field
                    ),
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
                "invalid_input",
                "所填来源身份尚未在本公司登记；先查已有来源或登记实际资料，再使用有效引用。",
                fact_issues=issues,
            )
        return [deepcopy(mapped[source_id]) for source_id in ids]
