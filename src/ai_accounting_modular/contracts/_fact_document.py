"""One strict immutable fact shard and the sources declared by that exact content."""

import json
from dataclasses import dataclass, field

from ._errors import ContractError
from ._fact_sources import _validated_paths
from ._validation import canonical_json, validate_contract


@dataclass(frozen=True, slots=True, init=False)
class FactDocumentPart:
    """Normal construction checks the complete installed FactShard before sorting."""

    _body: str = field(repr=False)
    key: str
    business_ids: tuple[str, ...]
    source_ids: tuple[str, ...]

    def __init__(self, part):
        try:
            value = json.loads(canonical_json(part))
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise ContractError("invalid_input", "事实文档片包含无效 JSON 类型或字符。") from error
        validate_contract("core.schema.json", "FactShard", value)
        _, family, period = value["key"].split(":")
        businesses, sources = set(), set()
        for fact in value["facts"]:
            identity = fact["business_id"]
            if identity in businesses:
                raise ContractError("invalid_input", "事实文档片不能重复保存同一最终业务。")
            if fact["kind"].split(".", 1)[0] != family or fact["period"] != period:
                raise ContractError("invalid_input", "事实的领域或月份与文档分片不一致。")
            businesses.add(identity)
            sources.update(_validated_paths(fact, "Fact"))
            # The complete schema has already rejected duplicate evidence IDs.
            fact["evidence_ids"].sort()
        value["facts"].sort(key=lambda fact: fact["business_id"])
        object.__setattr__(self, "_body", canonical_json(value))
        object.__setattr__(self, "key", value["key"])
        object.__setattr__(self, "business_ids", tuple(sorted(businesses)))
        object.__setattr__(self, "source_ids", tuple(sorted(sources)))

    @property
    def canonical_json(self) -> str:
        return self._body
