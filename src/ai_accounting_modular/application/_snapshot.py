"""Offline owner proofs over one database view, without executing business formulas."""

from copy import deepcopy
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, loads_json, validate_contract

from . import _documents
from ._changes import fact_digest
from ._context import DomainRegistry
from ._relations import identity as relation_identity


class _DirectFacts:
    """Declaration can inspect explicit facts, but cannot query or compute results."""

    def __init__(self, facts):
        self._facts = {fact["business_id"]: fact for fact in facts}

    def get_fact(self, business_id):
        return deepcopy(self._facts.get(business_id))


@dataclass(frozen=True)
class SnapshotVerifier:
    identity: object
    ledger: object
    records: object
    registry: DomainRegistry

    def verify(self, read):
        heads = read.repository("application").snapshot_heads()
        facts = self._facts(heads, read)
        self._relations(facts, heads, read)
        objects = _documents.export_objects(self, read)
        formal = self.ledger.verify_snapshot(read)
        indexed = {head["business_id"]: head for head in heads}
        actual_slots = {
            item.business_id: item.active_result_slots for item in formal.business_versions
        }
        for head in heads:
            try:
                expected = loads_json(head["result_slots_json"])
                if not isinstance(expected, list):
                    raise ValueError
                for slot in expected:
                    validate_contract("core.schema.json", "Slot", slot)
                if len(expected) != len(set(expected)) or head["state"] == "withdrawn" and expected:
                    raise ValueError
            except (ContractError, ValueError) as error:
                raise ContractError(
                    "storage_corrupt", "当前业务的完整结果责任目录不合法。"
                ) from error
            if tuple(sorted(expected)) != actual_slots.get(head["business_id"], ()):
                raise ContractError("storage_corrupt", "当前业务承诺的完整结果没有对应的正式采用。")
        for version in formal.business_versions:
            head = indexed.get(version.business_id)
            if (
                head is None
                or version.max_fact_seq > head["seq"]
                or version.open_fact_seq is not None
                and version.open_fact_seq != head["seq"]
            ):
                raise ContractError("storage_corrupt", "正式采用的事实版本与当前业务版本不一致。")
        return self.records.verify_snapshot(
            _documents.company_identity(read),
            facts,
            objects,
            read,
            required_source_ids=formal.source_ids,
        )

    def _relations(self, facts, heads, read):
        """Prove the rebuildable current directory without rerunning accounting."""
        indexed = {head["business_id"]: head for head in heads}
        grouped = {}
        for fact in facts:
            owner = self.registry.owner(fact["kind"])
            grouped.setdefault(owner, []).append(
                {
                    "op": "recalculate",
                    "business_id": fact["business_id"],
                    "expected_seq": indexed[fact["business_id"]]["seq"],
                    "fact": deepcopy(fact),
                }
            )
        expected, actual = set(), set()
        context = _DirectFacts(facts)
        for owner, changes in grouped.items():
            ids = {change["business_id"] for change in changes}
            for relation in owner.declare_relations(changes, context):
                validate_contract("business-facts.schema.json", "DirectRelation", relation)
                if relation["consumer_business_id"] not in ids:
                    raise ContractError("storage_corrupt", "直接关系声明不属于当前事实的负责模块。")
                expected.add(relation_identity(relation))
        repository = read.repository("application")
        ids = tuple(indexed)
        for offset in range(0, len(ids), 500):
            actual.update(
                relation_identity(row) for row in repository.relations(ids[offset : offset + 500])
            )
        if actual != expected:
            raise ContractError("storage_corrupt", "保存的直接关系目录与完整当前事实不一致。")

    def _facts(self, heads, read):
        """FKs cover absent identities; every owner also proves no misplaced body remains."""
        facts = []
        owners = dict.fromkeys(self.registry.owner(kind) for kind in self.registry.supported_kinds)
        for offset in range(0, len(heads), 500):
            batch = {head["business_id"]: head for head in heads[offset : offset + 500]}
            for owner in owners:
                exported = owner.export_current(list(batch), read)
                expected = {
                    key
                    for key, head in batch.items()
                    if head["state"] == "active" and self.registry.owner(head["kind"]) is owner
                }
                ids = [fact["business_id"] for fact in exported]
                if len(ids) != len(set(ids)) or set(ids) != expected:
                    raise ContractError(
                        "storage_corrupt", "领域正文有遗漏、重复或不属于本模块的记录。"
                    )
                for fact in exported:
                    validate_contract("business-facts.schema.json", "Fact", fact)
                    head = batch[fact["business_id"]]
                    if (
                        head["family"] != fact["kind"].split(".", 1)[0]
                        or head["kind"] != fact["kind"]
                        or head["period"] != fact["period"]
                        or head["current_digest"] != fact_digest(fact)
                    ):
                        raise ContractError("storage_corrupt", "领域正文与完整当前业务索引不一致。")
                    facts.append(fact)
        return facts
