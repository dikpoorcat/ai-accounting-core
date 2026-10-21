"""One strictly validated, detached fact and its exact derived source analysis."""

import json
from dataclasses import dataclass, field
from hashlib import sha256

from ._errors import ContractError
from ._fact_sources import _validated_paths
from ._validation import canonical_json, validate_contract
from ._wire_types import Fact


@dataclass(frozen=True, slots=True, init=False)
class FactAnalysis:
    """Immutable content proof; reading ``fact`` never exposes the stored value."""

    _body: str = field(repr=False)
    digest: str
    source_ids: tuple[str, ...]
    _paths: tuple[tuple[str, tuple[str, ...]], ...] = field(repr=False)

    def __init__(self, fact: Fact):
        try:
            body = canonical_json(fact)
            snapshot = json.loads(body)
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            raise ContractError("invalid_input", "事实包含无效 JSON 类型或字符") from error
        validate_contract("business-facts.schema.json", "Fact", snapshot)
        paths = _validated_paths(snapshot, "Fact")
        object.__setattr__(self, "_body", body)
        object.__setattr__(self, "digest", sha256(body.encode("utf-8")).hexdigest())
        object.__setattr__(self, "source_ids", tuple(paths))
        object.__setattr__(self, "_paths", tuple(paths.items()))

    @property
    def canonical_json(self) -> str:
        return self._body

    @property
    def fact(self) -> Fact:
        # Only this class's already validated canonical bytes are decoded.
        return json.loads(self._body)

    @property
    def source_paths(self) -> dict[str, tuple[str, ...]]:
        return dict(self._paths)


def analyze_fact(fact: Fact) -> FactAnalysis:
    return FactAnalysis(fact)
