"""One-submission overlays; formal identities remain the ledger's responsibility."""

from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from contextlib import contextmanager
from copy import deepcopy
from typing import TypeVar

from ai_accounting_modular.contracts.public import (
    ContractError,
    DomainOwner,
    EvaluatedSlot,
    InvestmentTaxEventView,
    ResourceCutoff,
    validate_contract,
)

from ._changes import fact_digest
from ._resources import check_scope as check_resource_scope

_CandidateValue = TypeVar("_CandidateValue")


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
        changed_business_ids=(),
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
        self._changed_business_ids = frozenset(changed_business_ids)
        self._plans = {}
        self._slots = {}
        self._fact_cache = {}
        self._result_cache = {}
        self._result_sources = {}
        self._candidate_values = None
        self._candidate_building = set()
        self._stored_results = {}
        self._dependencies = ()
        self._result_readers = {}
        self._declared_result_refs = {}
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

    def current_dependencies(self, business_ids):
        identities = tuple(business_ids)
        if len(identities) > 500:
            raise ContractError("invalid_input", "一次最多读取500个业务身份的依赖目录。")
        for identity in identities:
            validate_contract("core.schema.json", "Id", identity)
        return self.read_context.repository("application").dependencies(
            tuple(dict.fromkeys(identities))
        )

    @property
    def changed_business_ids(self):
        return self._changed_business_ids

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
        self._plans.clear()
        self._result_cache.clear()
        self._result_sources.clear()
        self._candidate_values = {}
        self._candidate_building.clear()
        self._dependencies = tuple(
            edge for _, description in descriptions for edge in description["dependencies"]
        )
        readers = defaultdict(set)
        references = defaultdict(lambda: defaultdict(set))
        for edge in self._dependencies:
            if edge["producer_kind"] == "business" and edge["read_mode"] == "result":
                consumer = edge["consumer_business_id"], edge["consumer_result_slot"]
                readers[edge["producer_id"], edge["producer_public_slot"]].add(consumer)
                references[edge["producer_id"], edge["relation_kind"]][consumer].add(
                    edge["producer_public_slot"]
                )
        self._result_readers = {key: frozenset(values) for key, values in readers.items()}
        self._declared_result_refs = {
            key: {consumer: tuple(sorted(slots)) for consumer, slots in values.items()}
            for key, values in references.items()
        }
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

    def end_candidate(self):
        self._candidate_values = None
        self._candidate_building.clear()
        self._plans.clear()
        self._result_cache.clear()
        self._result_sources.clear()
        self._candidate = None
        self._candidate_complete = False

    def candidate_memo(
        self, namespace: str, key: Hashable, build: Callable[[], _CandidateValue]
    ) -> _CandidateValue:
        if self._candidate_values is None or not self._active_slots:
            raise ContractError("invalid_operation", "共享计算只允许用于当前候选的结果求值或核验。")
        if not isinstance(namespace, str) or not namespace or not callable(build):
            raise ContractError("invalid_input", "共享计算须有明确职责名称和计算函数。")
        identity = namespace, key
        try:
            hash(identity)
        except TypeError as error:
            raise ContractError("invalid_input", "共享计算身份须为不可变、可比较的键。") from error
        if identity in self._candidate_building:
            raise ContractError("dependency_conflict", "共享计算不能循环依赖自身。")
        if identity not in self._candidate_values:
            self._candidate_building.add(identity)
            try:
                value = build()
            finally:
                self._candidate_building.remove(identity)
            self._candidate_values[identity] = value
        return self._candidate_values[identity]

    def is_result_pending(self, business_id, result_slot):
        if self._candidate_values is None:
            raise ContractError("invalid_operation", "只能检查当前候选承担的结果槽。")
        validate_contract(
            "core.schema.json",
            "BusinessRef",
            {
                "business_id": business_id,
                "slot": result_slot,
            },
        )
        return (business_id, result_slot) in self.pending_slots

    def require_result_sources(self, refs):
        if self._candidate_values is None or not self._active_slots:
            raise ContractError("invalid_operation", "正式来源核验须位于当前候选结果范围。")
        result = []
        for ref in refs:
            validate_contract("core.schema.json", "BusinessRef", ref)
            if not self._active_slots.intersection(
                self._result_readers.get((ref["business_id"], ref["slot"]), ())
            ):
                raise ContractError("dependency_conflict", "当前结果槽未声明采用该精确正式来源。")
            result.append(deepcopy(self._current_result_source(ref["business_id"], ref["slot"])))
        return tuple(result)

    def declared_result_refs(self, producer_id, relation_kind):
        if self._candidate_values is None or not self._active_slots:
            raise ContractError("invalid_operation", "声明来源只允许在当前候选结果范围读取。")
        validate_contract("core.schema.json", "Id", producer_id)
        readers = self._declared_result_refs.get((producer_id, relation_kind), {})
        slots = {slot for consumer in self._active_slots for slot in readers.get(consumer, ())}
        return tuple({"business_id": producer_id, "slot": slot} for slot in sorted(slots))

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

    def investment_tax_events(self, period_from, period_to, *, kinds=None):
        """Read only declared scopes, using the same candidate overlay as calculation."""
        available_kinds = (
            "investment.acquire",
            "investment.income",
            "investment.dispose",
            "investment.capital_return",
            "investment.reinvest",
            "investment.unit_change",
            "investment.loss",
            "investment.debt_interest",
            "investment.money_fund_purchase",
            "investment.money_fund_redemption",
            "opening.package",
        )
        kinds = available_kinds if kinds is None else tuple(kinds)
        if not set(kinds) <= set(available_kinds) or len(kinds) != len(set(kinds)):
            raise ContractError("invalid_input", "投资税务量只允许选择明确支持且不重复的事实种类。")
        self._periods(period_from, period_to)
        if period_from is None or period_to is None:
            raise ContractError("invalid_input", "投资税务量读取必须指定完整月份范围。")
        if not kinds:
            return ()
        edges = [
            edge
            for edge in self._dependencies
            if (edge["consumer_business_id"], edge["consumer_result_slot"]) in self._active_slots
        ]
        for kind in kinds:
            if not any(
                edge["producer_kind"] == "scope"
                and edge["producer_id"] == kind
                and edge["period_from"] <= period_from <= period_to <= edge["period_to"]
                for edge in edges
            ):
                raise ContractError("dependency_conflict", "投资税务量读取未声明完整事实范围。")
        result = []
        for fact in self.facts_in_scope(kinds, period_from, period_to):
            if not any(
                edge["producer_kind"] == "business"
                and edge["producer_id"] == fact["business_id"]
                and edge["producer_public_slot"] == "main"
                and edge["read_mode"] == "result"
                for edge in edges
            ):
                raise ContractError("dependency_conflict", "投资税务量未声明实际生产结果。")
            adopted = self.get_result(fact["business_id"], "main")
            if adopted is None or adopted.target["kind"] == "system.withdrawal":
                continue
            values = adopted.target["values"]
            events = values.get("tax_events", [])
            if "tax_event" in values:
                events = [values["tax_event"]]
            if fact["kind"] == "opening.package":
                events = [
                    {
                        "event_kind": "opening",
                        "terms_id": quantity["terms_id"],
                        "product_id": quantity["product_id"],
                        "tax_facts": deepcopy(quantity["tax_facts"]),
                        "quantity": quantity["quantity"],
                        "origin_ref": deepcopy(quantity["source"]),
                        **{
                            target: quantity[source]
                            for source, target in (
                                ("tax_buy_basis_fen", "opening_vat_buy_basis_fen"),
                                ("eit_tax_basis_fen", "opening_eit_tax_basis_fen"),
                                ("acquired_on", "acquired_on"),
                                ("acquisition_origin", "acquisition_origin"),
                                ("eit_interest_realized_through", "eit_interest_realized_through"),
                                ("eit_interest_source", "eit_interest_source"),
                                ("legal_form", "legal_form"),
                                ("quantity_unit", "quantity_unit"),
                                (
                                    "financial_instrument_reference",
                                    "financial_instrument_reference",
                                ),
                            )
                            if source in quantity
                        },
                    }
                    for quantity in adopted.basis.get("adopted_quantities", ())
                    if quantity["type"] == "investment_lot"
                ]
            elif (
                fact["kind"]
                in {"investment.money_fund_purchase", "investment.money_fund_redemption"}
                and not events
            ):
                raise ContractError(
                    "needs_information",
                    "旧货币基金事实没有投资税务性质和独立买入价依据，不能推定免税或税基。",
                    fact_issues=[
                        {
                            "code": "investment_tax_basis_required",
                            "business_id": fact["business_id"],
                            "field": "/data",
                            "meaning": "投资税务性质和独立计税基础",
                            "message": "需要从原投资资料核实真实权利、税务性质和买入价。",
                            "allowed_precision": [],
                            "reusable_sources": [
                                {"source_id": value} for value in fact["evidence_ids"]
                            ],
                            "blocking_scope": "business",
                        }
                    ],
                )
            for index, event in enumerate(events):
                result.append(
                    InvestmentTaxEventView(
                        deepcopy(event),
                        deepcopy(adopted.source),
                        adopted.basis["period"],
                        adopted.basis.get("effective_on"),
                        index,
                    )
                )
        return tuple(result)

    def pre_tax(self, period_from, period_to):
        self._check_aggregate_scope("pre_tax", period_from, period_to)
        return self._candidate.pre_tax(period_from, period_to)

    def complete_candidate(self):
        self._candidate.complete()
        self._candidate_complete = True

    def add_fact(self, fact):
        if self._candidate_values is not None:
            raise ContractError("invalid_operation", "候选开始后不能更换事实，须重新建立完整候选。")
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
        self._result_sources.pop(key, None)

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

    def _current_result_source(self, business_id, result_slot):
        key = business_id, result_slot
        if key not in self._result_sources:
            plan = self._plans.get(key)
            if business_id in self._slots and plan is None:
                if result_slot not in self._slots[business_id] or key not in self._retained_results:
                    raise ContractError(
                        "dependency_conflict", "同单所需结果槽尚未完成，须先声明正确依赖。"
                    )
            self._result_sources[key] = self._ledger.get_result_source(
                business_id, result_slot, self.read_context, plan=plan
            )
        return self._result_sources[key]

    def _load_result(self, business_id, result_slot):
        plan = self._plans.get((business_id, result_slot))
        if plan is not None:
            source = self._current_result_source(business_id, result_slot)
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
                self._current_result_source(business_id, result_slot),
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
            self._current_result_source(business_id, result_slot),
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
