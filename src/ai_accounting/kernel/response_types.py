"""Shared scalar types for strict read-response contracts."""

from typing import Annotated, Literal

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

from .types import Fen

WireFen = Annotated[
    Fen,
    PlainSerializer(str, return_type=str, when_used="json"),
    WithJsonSchema(
        {"type": "string", "pattern": r"^(0|-?[1-9][0-9]*)$", "x-fen-int64": True},
        mode="serialization",
    ),
]


def _integer_literal(value):
    if type(value) is not int:
        raise ValueError("integer required")
    return value


Version1 = Annotated[Literal[1], BeforeValidator(_integer_literal)]
Version2 = Annotated[Literal[2], BeforeValidator(_integer_literal)]
Version3 = Annotated[Literal[3], BeforeValidator(_integer_literal)]
Version4 = Annotated[Literal[4], BeforeValidator(_integer_literal)]
Version5 = Annotated[Literal[5], BeforeValidator(_integer_literal)]
Version6 = Annotated[Literal[6], BeforeValidator(_integer_literal)]
