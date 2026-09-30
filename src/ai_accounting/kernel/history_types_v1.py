"""Fixed v1 read identities and correction result value shapes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .content_v1 import _V1YearMonth as YearMonth
from .history_encoding_v1 import checked, sum_fen


@dataclass(frozen=True, order=True)
class Read:
    source: Literal["fact", "calculation"]
    kind: str
    key: str
    before_period: YearMonth | None = None


class CorrectionContext:
    """Declared exact-version reads needed by the two v1 opening corrections."""

    def __init__(self, selections):
        self._selections = {read: tuple(items) for read, items in selections.items()}

    def select(self, read: Read):
        if read not in self._selections:
            from .contracts import KernelError

            raise KernelError("undeclared_read", f"calculator did not declare {read}")
        return self._selections[read]

    def facts(self, kind: str, key: str):
        return self.select(Read("fact", kind, key))

    def calculations(self, kind: str, key: str):
        return self.select(Read("calculation", kind, key))

    def one(self, kind: str, key: str):
        from .contracts import KernelError, NeedsInformation

        items = self.facts(kind, key)
        if not items:
            raise NeedsInformation(kind, "需要已确认的核算来源", sources=(key,))
        if len(items) != 1:
            raise KernelError("ambiguous_source", f"multiple {kind} sources for {key}")
        return items[0]


@dataclass(frozen=True)
class Line:
    """The released v1 journal line shape used by historical correction checks."""

    account: str
    debit: int = 0
    credit: int = 0
    cashflow: str | None = None

    def __post_init__(self):
        checked(self.debit)
        checked(self.credit)
        if (
            not self.account
            or self.debit < 0
            or self.credit < 0
            or bool(self.debit) == bool(self.credit)
        ):
            raise ValueError("a line has exactly one positive side and an account")


@dataclass(frozen=True)
class BalanceEffect:
    key: str
    amount: int
    category: str = "payable"

    def __post_init__(self):
        checked(self.amount)


@dataclass(frozen=True)
class Outcome:
    """Only the persisted v1 result shape and amount checks, not a calculator."""

    lines: tuple[Line, ...]
    values: dict[str, Any]
    balances: tuple[BalanceEffect, ...] = ()
    explanation: tuple[dict, ...] = ()
    opening_lines: tuple[Line, ...] = ()
    opening: bool = False

    def __post_init__(self):
        if type(self.opening) is not bool or (self.opening_lines and not self.opening):
            raise ValueError("opening balances require an explicit opening calculation")
        if self.opening_lines and (self.lines or any(line.cashflow for line in self.opening_lines)):
            raise ValueError("opening balances cannot carry current-period activity or cash flow")
        if self.opening_lines and (
            len(self.opening_lines) < 2
            or sum_fen(x.debit for x in self.opening_lines)
            != sum_fen(x.credit for x in self.opening_lines)
        ):
            raise ValueError("unbalanced opening calculation")
        if self.lines and (
            len(self.lines) < 2
            or sum_fen(x.debit for x in self.lines) != sum_fen(x.credit for x in self.lines)
        ):
            raise ValueError("unbalanced calculation")
