"""Exact arithmetic and calendar values used inside module implementations."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

MIN_FEN = -(2**63)
MAX_FEN = 2**63 - 1
_FEN = re.compile(r"0|-?[1-9][0-9]*")
_MONTH = re.compile(r"(?!0000)[0-9]{4}-(0[1-9]|1[0-2])")
_DATE = re.compile(r"(?!0000)[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
_RATE = re.compile(r"0(\.[0-9]{1,18})?|1(\.0{1,18})?")
_PERCENT = re.compile(r"(0|[1-9][0-9]?)(\.[0-9]{1,18})?|100(\.0{1,18})?")


def checked_fen(value: int) -> int:
    if type(value) is not int or not MIN_FEN <= value <= MAX_FEN:
        raise ValueError("金额必须是有符号 64 位整数分")
    return value


def parse_fen(value: str) -> int:
    if type(value) is not str or len(value) > 20 or _FEN.fullmatch(value) is None:
        raise ValueError("金额必须是规范整数分字符串")
    return checked_fen(int(value))


def format_fen(value: int) -> str:
    return str(checked_fen(value))


def sum_fen(values: Iterable[int]) -> int:
    total = sum(checked_fen(value) for value in values)
    return checked_fen(total)


def parse_rate(value: str) -> Decimal:
    if type(value) is not str or _RATE.fullmatch(value) is None:
        raise ValueError("比率必须是 0 到 1、最多 18 位小数的规范字符串")
    return Decimal(value)


def parse_percent(value: str) -> Decimal:
    if type(value) is not str or _PERCENT.fullmatch(value) is None:
        raise ValueError("百分数必须是 0 到 100、最多 18 位小数的规范字符串")
    return Decimal(value)


class YearMonth(str):
    def __new__(cls, value: str):
        if not isinstance(value, str) or _MONTH.fullmatch(value) is None:
            raise ValueError("月份必须是有效的 YYYY-MM")
        return super().__new__(cls, value)

    @property
    def ordinal(self) -> int:
        return (int(self[:4]) - 1) * 12 + int(self[5:]) - 1

    @classmethod
    def from_ordinal(cls, ordinal: int) -> YearMonth:
        if type(ordinal) is not int or not 0 <= ordinal < 9999 * 12:
            raise ValueError("月份序号超出范围")
        year, month = divmod(ordinal, 12)
        return cls(f"{year + 1:04d}-{month + 1:02d}")


class ActualDate(str):
    def __new__(cls, value: str):
        if not isinstance(value, str) or _DATE.fullmatch(value) is None:
            raise ValueError("实际日期必须是有效的 YYYY-MM-DD")
        date.fromisoformat(value)
        return super().__new__(cls, value)

    @property
    def period(self) -> YearMonth:
        return YearMonth(self[:7])
