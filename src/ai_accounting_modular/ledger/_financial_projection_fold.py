"""Ordered sparse amounts preserve the owner's sequential overflow and unknowns."""

from __future__ import annotations

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import MAX_FEN, MIN_FEN, checked_fen

from ._balances import sum_amounts
from ._financial_cells import _MIXED_ISSUE
from ._values import fail


@dataclass(frozen=True)
class Notice:
    field: str
    code: str
    meaning: str
    message: str
    mixed_codes: bool = False

    def final(self):
        if not self.mixed_codes:
            return self
        return Notice(self.field, **_MIXED_ISSUE)

    def record(self):
        return [self.field, self.code, self.meaning, self.message, self.mixed_codes]

    @classmethod
    def from_record(cls, raw):
        if (
            type(raw) is not list
            or len(raw) != 5
            or any(type(value) is not str for value in raw[:4])
            or type(raw[4]) is not bool
        ):
            fail("storage_corrupt", "财务派生字段的未知依据格式损坏。")
        return cls(*raw)


def merge_notices(left, right):
    """Keep the first original code: the materialized fallback is not that code."""
    values = {value.field: value for value in left}
    for value in right:
        old = values.get(value.field)
        if old is None:
            values[value.field] = value
        elif old.mixed_codes or value.mixed_codes or old.code != value.code:
            values[value.field] = Notice(old.field, old.code, old.meaning, old.message, True)
    return tuple(values.values())


@dataclass(frozen=True)
class Unknown:
    notices: tuple[Notice, ...]
    resets_initial: bool = False


@dataclass(frozen=True)
class Fold:
    # Internal totals can exceed Fen: only a consumed sequential prefix is bounded.
    total: int = 0
    minimum: int = 0
    maximum: int = 0
    notices: tuple[Notice, ...] = ()
    resets_initial: bool = False

    @classmethod
    def one(cls, value):
        if isinstance(value, Unknown):
            return cls(notices=value.notices, resets_initial=value.resets_initial)
        checked_fen(value)
        return cls(value, min(0, value), max(0, value))

    def then(self, other):
        if self.notices:
            minimum, maximum = self.minimum, self.maximum
        else:
            minimum = min(self.minimum, self.total + other.minimum)
            maximum = max(self.maximum, self.total + other.maximum)
        return Fold(
            self.total + other.total,
            minimum,
            maximum,
            other.notices if other.resets_initial else merge_notices(self.notices, other.notices),
            self.resets_initial or other.resets_initial,
        )

    def finish(self, initial=0):
        if isinstance(initial, Unknown):
            return Unknown(
                self.notices
                if self.resets_initial
                else merge_notices(initial.notices, self.notices)
            )
        checked_fen(initial)
        if initial + self.minimum < MIN_FEN:
            sum_amounts((initial + self.minimum,))
        if initial + self.maximum > MAX_FEN:
            sum_amounts((initial + self.maximum,))
        return Unknown(self.notices) if self.notices else checked_fen(initial + self.total)

    def record(self):
        return [
            self.total,
            self.minimum,
            self.maximum,
            [note.record() for note in self.notices],
            self.resets_initial,
        ]

    @classmethod
    def from_record(cls, raw):
        if (
            type(raw) is not list
            or len(raw) != 5
            or any(type(value) is not int for value in raw[:3])
            or type(raw[3]) is not list
            or type(raw[4]) is not bool
            or (raw[4] and not raw[3])
            or not raw[1] <= 0 <= raw[2]
            or (not raw[3] and not raw[1] <= raw[0] <= raw[2])
        ):
            fail("storage_corrupt", "财务派生金额的有序累计格式损坏。")
        notices = tuple(Notice.from_record(note) for note in raw[3])
        if len({note.field for note in notices}) != len(notices):
            fail("storage_corrupt", "财务派生字段的未知依据重复。")
        return cls(*raw[:3], notices, raw[4])


def values_fold(values):
    result = Fold()
    for value in values:
        result = result.then(Fold.one(value))
    return result


def combine(left, right):
    return {
        key: left.get(key, Fold()).then(right.get(key, Fold()))
        for key in left.keys() | right.keys()
    }
