"""The public entry point for shared wire types and process-local contracts."""

from ._contexts import EvaluatedSlot, EvaluationContext, ReadContext, UnitOfWork
from ._errors import ContractError
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
    "UnitOfWork",
    "ContractError",
    "checked_fen",
    "format_fen",
    "parse_fen",
    "parse_percent",
    "parse_rate",
    "sum_fen",
    "canonical_json",
    "content_digest",
    "loads_json",
    "schema_document",
    "validate_contract",
]
