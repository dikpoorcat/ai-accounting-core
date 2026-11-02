"""Active source indexes and incremental FIFO groups for one investment replay."""

from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
from heapq import heappop, heappush

from ._common import fail, missing, total
from ._investment_math import quantity_total, reference_key


@contextmanager
def _ordered(heap, valid):
    removed, seen = [], set()

    def values():
        while heap:
            value = heappop(heap)
            if value in seen or not valid(value):
                continue
            seen.add(value)
            removed.append(value)
            yield value

    try:
        yield values()
    finally:
        for value in removed:
            heappush(heap, value)


class _Group:
    def __init__(self):
        self.members = {}
        self.sequence_counts = Counter()
        self.prices = Counter()
        self.quantity = Decimal(0)
        self._sequence_heap, self._stable_heap = [], []
        self._version = 0
        self._duplicate_sequences = 0

    def add(self, root, lot, units):
        self._version += 1
        price = Fraction(lot.original_cost) / Fraction(lot.original_quantity)
        self.members[root] = lot, units, price, self._version
        self.quantity = quantity_total((self.quantity, units))
        self._duplicate_sequences += self.sequence_counts[lot.acquisition_sequence] == 1
        self.sequence_counts[lot.acquisition_sequence] += 1
        self.prices[price] += 1
        heappush(self._stable_heap, (lot.acquired_on or "", root, self._version))
        heappush(
            self._sequence_heap,
            (lot.acquisition_sequence or 0, lot.acquired_on or "", root, self._version),
        )

    def remove(self, root):
        lot, units, price, _ = self.members.pop(root)
        self.quantity = quantity_total((self.quantity, units.copy_negate()))
        self._duplicate_sequences -= self.sequence_counts[lot.acquisition_sequence] == 2
        for counter, key in (
            (self.sequence_counts, lot.acquisition_sequence),
            (self.prices, price),
        ):
            counter[key] -= 1
            if not counter[key]:
                del counter[key]

    @property
    def known_sequence(self):
        return None not in self.sequence_counts and not self._duplicate_sequences

    def ordered(self):
        heap = self._sequence_heap if self.known_sequence else self._stable_heap
        with _ordered(
            heap,
            lambda key: key[-2] in self.members and self.members[key[-2]][3] == key[-1],
        ) as keys:
            for key in keys:
                yield self.members[key[-2]][0]


class _Month:
    def __init__(self):
        self.all = _Group()
        self.days, self.day_heap = {}, []
        self.unknown = 0

    def add(self, root, lot, units):
        self.all.add(root, lot, units)
        day = lot.acquired_on or ""
        group = self.days.setdefault(day, _Group())
        if not group.members:
            heappush(self.day_heap, day)
        group.add(root, lot, units)
        self.unknown += lot.acquired_on is None

    def remove(self, root):
        lot = self.all.members[root][0]
        self.all.remove(root)
        self.days[lot.acquired_on or ""].remove(root)
        self.unknown -= lot.acquired_on is None

    def groups(self):
        if self.unknown:
            yield self.all
        else:
            with _ordered(self.day_heap, lambda day: bool(self.days[day].members)) as days:
                for day in days:
                    yield self.days[day]


class ActiveLots:
    """Only live lots are retained; history and formal source amounts stay elsewhere."""

    def __init__(self, initial=()):
        self._sources, self._roots, self._months, self._month_heap = {}, {}, {}, []
        self.quantity, self.cost = Decimal(0), 0
        for lot in initial:
            self.append(lot)

    def __iter__(self):
        return (row[0] for row in self._sources.values())

    def _remove(self, key):
        lot, units, cost = self._sources.pop(key)
        root = reference_key(lot.origin_source or lot.source)
        self._months[lot.acquired_period].remove(root)
        if self._roots.get(root) == key:
            del self._roots[root]
        self.quantity = quantity_total((self.quantity, units.copy_negate()))
        self.cost = total((self.cost, -cost))

    def append(self, lot):
        if lot.remaining_quantity <= 0:
            fail("投资批次须有实际正数量，不能以零数量构造库存来源。", "invalid_input")
        root, key = reference_key(lot.origin_source or lot.source), reference_key(lot.source)
        previous = self._roots.get(root)
        if previous is not None:
            if self._sources[previous][0].remaining_quantity:
                fail("同一投资原批次不能同时存在两个有效成本区间。", "invalid_input")
            self._remove(previous)
        self._sources[key] = lot, lot.remaining_quantity, lot.remaining_cost
        self._roots[root] = key
        month = self._months.setdefault(lot.acquired_period, _Month())
        if not month.all.members:
            heappush(self._month_heap, lot.acquired_period)
        month.add(root, lot, lot.remaining_quantity)
        self.quantity = quantity_total((self.quantity, lot.remaining_quantity))
        self.cost = total((self.cost, lot.remaining_cost))

    def synchronize(self, uses):
        for use in uses:
            key = reference_key(use["source"])
            if key not in self._sources:
                continue
            lot = self._sources[key][0]
            self._remove(key)
            if lot.remaining_quantity:
                self.append(lot)

    def find(self, source):
        key = reference_key(source)
        row = self._sources.get(key) or self._sources.get(self._roots.get(key))
        if row is None or row[0].remaining_quantity <= 0:
            fail("所选投资来源没有唯一的当前剩余批次，须核对原始批次与实际调整。")
        return row[0]

    def fifo(self, required, origins):
        with _ordered(
            self._month_heap, lambda month: bool(self._months[month].all.members)
        ) as months:
            for month in months:
                for group in self._months[month].groups():
                    if (
                        required < group.quantity
                        and len(group.prices) > 1
                        and not group.known_sequence
                    ):
                        lot = next(iter(group.members.values()))[0]
                        origin = lot.origin_source or lot.source
                        fact = origins.get(origin["business_id"])
                        missing(
                            fact or {"business_id": origin["business_id"], "evidence_ids": []},
                            "/data/transaction_sequence"
                            if fact is not None or origin["slot"] == "cost"
                            else "/data/members",
                            "本次先进先出部分退出涉及同日或未知实际日的不同单价批次，"
                            "须从原取得资料确定真实日期或取得顺序；期初资料行序不能代替经济顺序。",
                            precision=("day", "sequence"),
                        )
                    for lot in group.ordered():
                        yield lot
                        required = quantity_total(
                            (required, min(required, lot.remaining_quantity).copy_negate())
                        )
                        if not required:
                            return
