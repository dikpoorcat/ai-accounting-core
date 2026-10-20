"""The public entry point for shared wire types and process-local contracts."""

from ._allocation import cumulative_shares
from ._contexts import (
    EvaluatedSlot,
    EvaluationContext,
    ReadContext,
    ResourceCutoff,
    ResourceDefinition,
    ResourceKind,
    ResourceState,
    ResourceUse,
    UnitOfWork,
)
from ._errors import ContractError
from ._fact_sources import fact_source_ids, fact_source_paths
from ._scalars import (
    MAX_FEN,
    MIN_FEN,
    ActualDate,
    YearMonth,
    checked_fen,
    format_fen,
    parse_fen,
    parse_percent,
    parse_rate,
    sum_fen,
)
from ._validation import (
    CONTRACT,
    canonical_json,
    content_digest,
    loads_json,
    schema_document,
    validate_contract,
)
from ._wire_types import *  # noqa: F403 - generated exports are the authoritative field types
from ._wire_types import __all__ as _wire_exports

__all__ = [
    *_wire_exports,
    "CONTRACT",
    "MAX_FEN",
    "MIN_FEN",
    "ActualDate",
    "YearMonth",
    "EvaluatedSlot",
    "EvaluationContext",
    "ReadContext",
    "ResourceCutoff",
    "ResourceDefinition",
    "ResourceKind",
    "ResourceState",
    "ResourceUse",
    "UnitOfWork",
    "ContractError",
    "fact_source_ids",
    "fact_source_paths",
    "checked_fen",
    "format_fen",
    "parse_fen",
    "parse_percent",
    "parse_rate",
    "sum_fen",
    "cumulative_shares",
    "canonical_json",
    "content_digest",
    "loads_json",
    "schema_document",
    "validate_contract",
]
