"""Shared wire/storage types. Monetary arithmetic never passes through float."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Annotated, Any

from pydantic import Field, StrictInt
from pydantic_core import core_schema

MIN_FEN = -(2**63)
MAX_FEN = 2**63 - 1
Fen = Annotated[StrictInt, Field(ge=MIN_FEN, le=MAX_FEN)]
NonNegativeFen = Annotated[Fen, Field(ge=0)]
PositiveFen = Annotated[Fen, Field(gt=0)]


def checked(value: int) -> int:
    if type(value) is not int or not MIN_FEN <= value <= MAX_FEN:
        raise ValueError("amount must be a signed 64-bit integer number of fen")
    return value


def sum_fen(values) -> int:
    result = 0
    for value in values:
        result = checked(result + checked(value))
    return result


class YearMonth(str):
    def __new__(cls, value: str):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("month must be YYYY-MM")
        if not 1 <= int(value[:4]) <= 9999:
            raise ValueError("year out of range")
        return super().__new__(cls, value)

    @property
    def ordinal(self) -> int:
        return (int(self[:4]) - 1) * 12 + int(self[5:]) - 1

    @classmethod
    def from_ordinal(cls, ordinal: int) -> YearMonth:
        if type(ordinal) is not int or not 0 <= ordinal < 9999 * 12:
            raise ValueError("invalid month ordinal")
        year, month = divmod(ordinal, 12)
        return cls(f"{year + 1:04d}-{month + 1:02d}")

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(strict=True, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        from typing import get_args

        from ai_accounting.fact_requirements import RecognitionPeriod

        return handler(schema) | get_args(RecognitionPeriod)[1].json_schema_extra


class ActualDate(str):
    """An actual business day. A recognition month cannot be converted implicitly."""

    def __new__(cls, value: str):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("actual date must be YYYY-MM-DD")
        date.fromisoformat(value)
        return super().__new__(cls, value)

    @property
    def period(self) -> YearMonth:
        return YearMonth(self[:7])

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return core_schema.no_info_after_validator_function(
            cls, core_schema.str_schema(strict=True, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        return handler(schema) | {
            "x-accounting-fact": {
                "role": "accounting",
                "meaning": "actual_business_day",
                "precision": "day",
            }
        }


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> bytes:
    return hashlib.sha256(canonical(value).encode("utf-8")).digest()
