"""Sparse immutable latest-month and latest-tax-period financial VAT selections."""

from ai_accounting_modular.storage.public import Entry, Tree

from ._balances import sum_amounts
from ._financial_projection_indexes import PlainStrategy, order_key, prefix_end
from ._financial_tax_positions import validate_row
from ._values import fail


def period_key(row):
    return order_key(row[2:4])


def entry_key(atom, row, *, by_period):
    prefix = period_key(row) if by_period else ""
    return prefix + order_key((row[1], "1" if row[0] == "assessment" else "0", atom))


class Latest:
    def __init__(self, bodies, *, by_period):
        self.bodies, self.by_period = bodies, by_period

    def _value(self, atom):
        body = self.bodies.get_many((atom,))[0]
        row = body["financial_tax"]
        if row is None or not self.by_period and row[0] != "position":
            fail("storage_corrupt", "金融税组成索引没有对应的正式位置。")
        return [period_key(row) if self.by_period else "", atom, row]

    def leaf(self, entries):
        values = []
        for entry in entries:
            if type(entry.value) is not str:
                fail("storage_corrupt", "金融税组成索引的精确来源损坏。")
            value = self._value(entry.value)
            if entry.key != entry_key(value[1], value[2], by_period=self.by_period):
                fail("storage_corrupt", "金融税组成索引与正式来源顺序不一致。")
            values.append(value)
        if not values or any(value[0] != values[0][0] for value in values):
            return None
        orders = [(value[2][1], value[2][0]) for value in values]
        if len(orders) != len(set(orders)):
            fail("storage_corrupt", "同一金融税月份或纳税期存在重复采用。")
        return values[-1]

    def branch(self, children):
        values = [child.summary for child in children]
        if not values or any(value is None or value[0] != values[0][0] for value in values):
            return None
        return values[-1]

    @staticmethod
    def encode_summary(value):
        return value

    def decode_summary(self, raw):
        if raw is None:
            return None
        if (
            type(raw) is not list
            or len(raw) != 3
            or type(raw[0]) is not str
            or type(raw[1]) is not str
        ):
            fail("storage_corrupt", "金融税组成摘要损坏。")
        validate_row(raw[2])
        if raw[0] != (period_key(raw[2]) if self.by_period else ""):
            fail("storage_corrupt", "金融税组成摘要的纳税期不一致。")
        return raw


class TaxPositions:
    def __init__(self, store, bodies, saved=None):
        self.trees = {
            "carry": Tree("financial-tax-carry/1", Latest(bodies, by_period=False), store),
            "periods": Tree("financial-tax-period/1", Latest(bodies, by_period=True), store),
            "unassessed": Tree("financial-tax-unassessed/1", PlainStrategy(), store),
        }
        if saved is not None and (type(saved) is not dict or set(saved) != set(self.trees)):
            fail("storage_corrupt", "金融税组成根目录不完整。")
        self.roots = {
            name: tree.root(saved[name] if saved else None) for name, tree in self.trees.items()
        }

    def wire(self):
        return {name: root.wire() if root else None for name, root in self.roots.items()}

    def root_ids(self):
        return tuple(root.digest for root in self.roots.values() if root)

    def replace(self, values):
        changes = {name: {} for name in self.trees}
        touched = set()
        for atom, body, sign in values:
            row = body["financial_tax"]
            if row is None:
                continue
            dimension = period_key(row)
            touched.add(dimension)
            for name in ("periods", "carry") if row[0] == "position" else ("periods",):
                key = entry_key(atom, row, by_period=name == "periods")
                changes[name][key] = Entry(key, atom) if sign > 0 else None
        for name in ("periods", "carry"):
            self.roots[name] = self.trees[name].patch(self.roots[name], changes[name])
        for key in touched:
            value = self.trees["periods"].summary(
                self.roots["periods"], first=key, last=prefix_end(key)
            )
            if value is not None and value[0] != key:
                fail("storage_corrupt", "金融税纳税期没有对应的正式摘要。")
            changes["unassessed"][key] = (
                Entry(key, value) if value and value[2][0] == "position" and value[2][5] else None
            )
        self.roots["unassessed"] = self.trees["unassessed"].patch(
            self.roots["unassessed"], changes["unassessed"]
        )

    def values(self):
        latest = self.trees["carry"].summary(self.roots["carry"])
        unpaid = 0
        for entry in self.trees["unassessed"].entries(self.roots["unassessed"]):
            value = self.trees["periods"].strategy.decode_summary(entry.value)
            if value is None or value[0] != entry.key or value[2][0] != "position":
                fail("storage_corrupt", "未申报金融税负没有唯一的纳税期来源。")
            unpaid = sum_amounts((unpaid, value[2][5]))
        return latest[2][4] if latest else 0, unpaid
