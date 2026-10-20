"""Indexed public resources on one persisted snapshot and its candidate overlay."""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ResourceDefinition,
    format_fen,
    validate_contract,
)

from ._planning import stored_source_reference
from ._resource_rows import resource_records
from ._values import fail

_DEFINITION_TABLES = {
    "obligation": "obligation",
    "cost": "cost_lot",
    "funds": "result_fund_movement",
}
_INCOMING_TABLES = {
    "obligation": ("settlement", "obligation_reclassification"),
    "cost": ("cost_consumption",),
}


def _load_groups(cache, keys, fetch, field):
    missing = tuple(dict.fromkeys(key for key in keys if key not in cache))
    if missing:
        grouped = {key: [] for key in missing}
        for row in fetch(missing):
            grouped[row[field]].append(row)
        cache.update(grouped)
    return [row for key in keys for row in cache[key]]


class ResourceRepository:
    """One unit's persisted resources, optionally shadowed by complete responsibilities.

    Definitions are validated once per result. Incoming edges are indexed by the
    public source, never by a cached balance: every cutoff still sees its own rows.
    Ledger writes invalidate persisted indexes; staged rows stay in this candidate.
    """

    def __init__(self, base, responsibilities=(), *, as_of=None):
        self.base, self.as_of = base, as_of
        self.responsibilities = {item["business_id"]: item for item in responsibilities}
        old_heads = (
            base.current_heads(tuple(self.responsibilities)) if self.responsibilities else []
        )
        self.shadowed = {row["result_id"] for row in old_heads}
        self.heads, self.records, self.sources = {}, {}, {}
        self._staged_heads = defaultdict(list)
        self._staged_incoming = defaultdict(lambda: defaultdict(list))
        self._staged_movements = defaultdict(list)
        self._staged_settlements = defaultdict(list)
        self._staged_accounts = defaultdict(list)
        self._revision, self._staged_revision = base.resource_revision, 0
        self._clear_persisted()

    def _clear_persisted(self):
        self._stored_heads = {}
        self._business_heads = {}
        self._children = defaultdict(dict)
        self._incoming = defaultdict(dict)
        self._incoming_slots = {}
        self._movements, self._settlements, self._scopes = {}, {}, {}
        self._definitions, self._business_definitions = {}, {}
        self._loaded_definitions = set()
        self.sources = {key: value for key, value in self.sources.items() if key in self.heads}

    def _refresh(self):
        if self._revision != self.base.resource_revision:
            self._clear_persisted()
            self._revision = self.base.resource_revision

    @property
    def version(self):
        self._refresh()
        return self._revision, self._staged_revision

    def add(self, plan, bound):
        self._refresh()
        identity = bound.identity
        row = {
            "result_id": identity.result_id,
            "business_id": plan["business_id"],
            "result_slot": plan["result_slot"],
            "business_period": plan["accounting_period"],
            "target_json": json.dumps(plan["target"]),
            "basis_json": json.dumps(plan["basis"]),
        }
        self.heads[identity.result_id] = row
        self._staged_heads[row["business_id"]].append(row)
        self.shadowed.add(identity.result_id)
        tables = resource_records(identity.result_id, plan)
        self.records[identity.result_id] = tables
        self.sources[identity.result_id] = deepcopy(bound.source_ref)
        for table in ("settlement", "cost_consumption", "obligation_reclassification"):
            for edge in tables[table]:
                ref = edge["source_business_id"], edge["source_public_slot"]
                self._staged_incoming[table][ref].append(edge)
                if table == "settlement" and edge["movement_id"] is not None:
                    self._staged_settlements[edge["movement_id"]].append(edge)
        for movement in tables["result_fund_movement"]:
            self._staged_movements[movement["movement_id"]].append(movement)
            self._staged_accounts[movement["account_id"]].append(movement)
        for kind in _DEFINITION_TABLES:
            self._definitions.pop((kind, identity.result_id), None)
            self._loaded_definitions.discard((kind, identity.result_id))
            self._business_definitions.pop((kind, plan["business_id"]), None)
        self._staged_revision += 1

    def current_heads(self, business_ids):
        self._refresh()

        def fetch(ids):
            if self.as_of is not None:
                return self.base.heads_through(self.as_of, ids)
            return self.base.current_heads(ids)

        rows = _load_groups(self._business_heads, business_ids, fetch, "business_id")
        staged = [row for key in business_ids for row in self._staged_heads[key]]
        self._stored_heads.update((row["result_id"], row) for row in rows)
        return [row for row in rows if row["result_id"] not in self.shadowed] + staged

    def result(self, result_id):
        self._refresh()
        if result_id in self.heads:
            return self.heads[result_id]
        if result_id not in self._stored_heads:
            self._stored_heads[result_id] = self.base.result(result_id)
        return self._stored_heads[result_id]

    def source(self, result_id):
        self._refresh()
        if result_id not in self.sources:
            self.sources[result_id] = stored_source_reference(self.result(result_id))
        return deepcopy(self.sources[result_id])

    def children(self, table, result_ids):
        self._refresh()
        persisted_ids = [key for key in result_ids if key not in self.records]
        rows = _load_groups(
            self._children[table],
            persisted_ids,
            lambda ids: self.base.children(table, ids),
            "result_id",
        )
        return rows + [
            row for key in result_ids for row in self.records.get(key, {}).get(table, ())
        ]

    def definition_maps(self, heads, kind):
        self._refresh()
        missing = [
            head for head in heads if (kind, head["result_id"]) not in self._loaded_definitions
        ]
        if missing:
            by_id = {head["result_id"]: head for head in missing}
            seen = {key: set() for key in by_id}
            for row in self.children(_DEFINITION_TABLES[kind], tuple(by_id)):
                slots = seen[row["result_id"]]
                if row["public_slot"] in slots:
                    fail("dependency_conflict", "同一业务的公开资源槽不能重复。")
                slots.add(row["public_slot"])
                self.definition(kind, by_id[row["result_id"]], row)
            self._loaded_definitions.update((kind, key) for key in by_id)
        businesses = defaultdict(list)
        for head in heads:
            businesses[head["business_id"]].append(head["result_id"])
        mapped = {}
        for business_id, result_ids in businesses.items():
            key, signature = (kind, business_id), tuple(sorted(result_ids))
            cached = self._business_definitions.get(key)
            if cached is None or cached[0] != signature:
                slots = {}
                for result_id in result_ids:
                    for slot, definition in self._definitions.get((kind, result_id), {}).items():
                        if slot in slots:
                            fail("dependency_conflict", "同一业务的公开资源槽不能重复。")
                        slots[slot] = definition
                cached = signature, slots
                self._business_definitions[key] = cached
            mapped[business_id] = cached[1]
        return mapped

    def definition(self, kind, head, row):
        self._refresh()
        slots = self._definitions.setdefault((kind, row["result_id"]), {})
        if row["public_slot"] not in slots:
            slots[row["public_slot"]] = resource_definition(
                kind, head, row, self.source(row["result_id"])
            )
        return slots[row["public_slot"]]

    def _persisted_incoming(self, table, business_ids):
        self._refresh()
        return _load_groups(
            self._incoming[table],
            business_ids,
            lambda ids: self.base.incoming(table, ids),
            "source_business_id",
        )

    def incoming_slot(self, table, business_id, slot):
        self._refresh()
        key = table, business_id
        if key not in self._incoming_slots:
            grouped = defaultdict(list)
            for row in self._persisted_incoming(table, (business_id,)):
                if row["result_id"] not in self.shadowed:
                    grouped[row["source_public_slot"]].append(row)
            self._incoming_slots[key] = grouped
        return self._incoming_slots[key][slot] + self._staged_incoming[table][business_id, slot]

    def uses(self, definition):
        if definition.kind == "funds":
            return [
                ("settlement", self.settlements_by_movements([definition.value["movement_id"]]))
            ]
        ref = definition.reference
        return [
            (table, self.incoming_slot(table, ref["business_id"], ref["slot"]))
            for table in _INCOMING_TABLES[definition.kind]
        ]

    def prepare_states(self, resources):
        """Batch missing persisted rows; selected slots keep later state work local."""
        grouped = defaultdict(list)
        for resource in resources:
            grouped[resource.kind].append(resource)
        for kind, items in grouped.items():
            if kind == "funds":
                self.settlements_by_movements([item.value["movement_id"] for item in items])
            else:
                ids = tuple(dict.fromkeys(item.reference["business_id"] for item in items))
                for table in _INCOMING_TABLES[kind]:
                    self._persisted_incoming(table, ids)
        movement_ids = {
            row["movement_id"]
            for resource in resources
            for table, rows in self.uses(resource)
            if table == "settlement"
            for row in rows
            if row["method"] == "money"
        }
        if movement_ids:
            self.movements(movement_ids)

    def _indexed_overlay(self, keys, cache, fetch, field, staged):
        rows = _load_groups(cache, keys, fetch, field)
        return [row for row in rows if row["result_id"] not in self.shadowed] + [
            row for key in keys for row in staged[key]
        ]

    def movements(self, movement_ids):
        self._refresh()
        return self._indexed_overlay(
            movement_ids,
            self._movements,
            self.base.movements,
            "movement_id",
            self._staged_movements,
        )

    def settlements_by_movements(self, movement_ids):
        self._refresh()
        return self._indexed_overlay(
            movement_ids,
            self._settlements,
            self.base.settlements_by_movements,
            "movement_id",
            self._staged_settlements,
        )

    def movement_scope(self, account_id, period_from, period_to):
        self._refresh()
        key = account_id, period_from, period_to
        if key not in self._scopes:
            rows = self.base.movement_scope(account_id, period_from, period_to, as_of=self.as_of)
            for row in rows:
                if row["result_id"] not in self.sources:
                    self.sources[row["result_id"]] = stored_source_reference(row)
            self._scopes[key] = rows
        return [row for row in self._scopes[key] if row["result_id"] not in self.shadowed] + [
            row
            for row in self._staged_accounts[account_id]
            if period_from <= row["period"] <= period_to
        ]


def validate_resource_refs(refs, kind):
    if kind not in _DEFINITION_TABLES:
        fail("invalid_input", "未知的公开资源类型。")
    if len(refs) > 500:
        fail("invalid_input", "一次最多读取500个公开资源。")
    for ref in refs:
        validate_contract("core.schema.json", "BusinessRef", ref)


def resource_definition(kind, head, row, source):
    reference = {"business_id": head["business_id"], "slot": row["public_slot"]}
    if kind == "funds":
        value = {
            "movement_id": row["movement_id"],
            "public_slot": row["public_slot"],
            "funds_account_id": row["account_id"],
            "actual_on": row["actual_on"],
            "direction": {"in": "inflow", "out": "outflow"}[row["direction"]],
            "amount_fen": format_fen(row["amount_fen"]),
        }
        for key in ("party_id", "transfer_group_id"):
            if row[key] is not None:
                value[key] = row[key]
        if row["cashflow_parts_json"] is not None:
            value["cashflow_parts"] = json.loads(row["cashflow_parts_json"])
    else:
        fields = (
            ("direction", "category", "account_code", "business_period")
            if kind == "obligation"
            else ("object_id", "category", "business_period")
        )
        value = {
            **reference,
            **{key: row[key] for key in fields},
            "amount_fen": format_fen(row["amount_fen"]),
        }
        if kind == "obligation":
            value["allowed_settlements"] = json.loads(row["allowed_settlements_json"])
            for key in ("party_id", "due_on"):
                if row[key] is not None:
                    value[key] = row[key]
            if row["cashflow_parts_json"] is not None:
                value["cashflow_parts"] = json.loads(row["cashflow_parts_json"])
    return ResourceDefinition(kind, reference, source, value)


def definitions(repo, heads, refs, kind):
    mapped = repo.definition_maps(heads, kind)
    result = []
    for ref in refs:
        value = mapped.get(ref["business_id"], {}).get(ref["slot"])
        if value is None:
            fail("dependency_conflict", "所需公开资源尚未完成、已经退出或不存在。")
        result.append(deepcopy(value))
    return tuple(result)


def fund_movements(repo, account_id, period_from, period_to):
    validate_contract("core.schema.json", "Id", account_id)
    validate_contract("core.schema.json", "Month", period_from)
    validate_contract("core.schema.json", "Month", period_to)
    if period_from > period_to:
        fail("invalid_input", "真实资金查询起月不能晚于末月。")
    result, seen = [], set()
    for row in repo.movement_scope(account_id, period_from, period_to):
        if row["movement_id"] in seen:
            fail("dependency_conflict", "同一实际资金发生不能由多个当前结果重复采用。")
        seen.add(row["movement_id"])
        head = repo.heads.get(row["result_id"]) or row
        result.append(deepcopy(repo.definition("funds", head, row)))
    result.sort(
        key=lambda item: (
            item.value["actual_on"],
            item.reference["business_id"],
            item.reference["slot"],
        )
    )
    return tuple(result)
