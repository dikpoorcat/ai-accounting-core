"""Selected journal and flow roles share immutable nodes across actual report ranges."""

from collections import Counter

from . import _financial_projection_chains as chains
from ._financial_order import target_key
from ._financial_projection_bodies import (
    resource_projection,
    source_atom,
    validate_resource_projection,
)
from ._financial_projection_fold import Fold
from ._financial_projection_indexes import (
    CashBalanceStrategy,
    FlowStrategy,
    JournalStrategy,
    PlainStrategy,
    body_head,
    dimension_key,
    flow_entries,
    flow_source_used,
    journal_key,
    order_key,
    prefix_end,
)
from ._financial_tree import Entry, Tree
from ._values import fail


def _wire(root):
    return root.wire() if root else None


def _atoms(heads):
    return tuple(dict.fromkeys(head["atom"] for head in heads if head is not None))


def _counts(saved):
    if type(saved) is not dict or any(
        type(period) is not str or type(n) is not int or n < 1 for period, n in saved.items()
    ):
        fail("storage_corrupt", "财务采用补差月份目录损坏。")
    return Counter(saved)


def _count_body(counts, body, sign, used):
    if used and body["baseline_result_id"]:
        period = body["posting_period"]
        counts[period] += sign
        if counts[period] < 0:
            fail("storage_corrupt", "财务采用补差月份计数损坏。")
        if not counts[period]:
            del counts[period]


class StockRole:
    def __init__(self, spec, store, bodies, saved=None):
        self.spec, self.bodies = spec, bodies
        self.trees = {
            "journal": Tree("financial-gl/1", JournalStrategy(bodies), store),
            "dimensions": Tree("financial-gl-dimensions/2", PlainStrategy(), store),
            "cash": Tree("financial-cash-before/1", CashBalanceStrategy(bodies), store),
            "resources": Tree("financial-controlled-resources/2", PlainStrategy(), store),
        }
        if saved is not None and set(saved) != {*self.trees, "supplements"}:
            fail("storage_corrupt", "财务存量角色的根目录不完整。")
        self.roots = {
            name: tree.root(saved[name] if saved is not None else None)
            for name, tree in self.trees.items()
        }
        self.supplements = _counts(saved["supplements"] if saved is not None else {})

    def wire(self):
        return {name: _wire(root) for name, root in self.roots.items()} | {
            "supplements": dict(self.supplements)
        }

    def root_ids(self):
        return tuple(root.digest for root in self.roots.values() if root)

    def patch(self, changed_chains):
        selected = [
            (
                chains.stock(old, self.spec["view"], self.spec["cutoff"]),
                chains.stock(new, self.spec["view"], self.spec["cutoff"]),
            )
            for old, new in changed_chains
        ]
        self._replace(selected)

    def extend(self, values, previous_spec=None):
        self._replace(
            [
                (
                    chains.stock(chain, previous_spec["view"], previous_spec["cutoff"])
                    if previous_spec
                    else None,
                    chains.stock(chain, self.spec["view"], self.spec["cutoff"]),
                )
                for chain in values
            ]
        )

    def _replace(self, selected):
        selected = [(old, new) for old, new in selected if old != new]
        if not selected:
            return
        self.bodies.get_many(_atoms(head for pair in selected for head in pair))
        updates = {name: {} for name in self.trees}
        dimensions, parts = Counter(), {}
        for old, new in selected:
            for head, sign in ((old, -1), (new, 1)):
                if head is None:
                    continue
                atom = head["atom"]
                body = self.bodies.get_many((atom,))[0]
                _count_body(self.supplements, body, sign, bool(body["journal"]))
                for row in body["journal"]:
                    key = journal_key(body, row)
                    updates["journal"][key] = Entry(key, [atom, row[0]]) if sign == 1 else None
                    dimension = dimension_key(*row[1:4])
                    dimensions[dimension] += sign
                    parts[dimension] = row[1:4]
                    if row[1] in ("1001", "1002", "1012"):
                        key = order_key((*target_key(head), f"{row[0]:019d}"))
                        updates["cash"][key] = Entry(key, [atom, row[0]]) if sign == 1 else None
                if any(body["resources"].values()):
                    key = chains.selected_key(head)
                    updates["resources"][key] = (
                        Entry(key, resource_projection(body)) if sign == 1 else None
                    )
        # A replacement can change an amount or its economic order without
        # changing the number of rows. Rebuild each touched dimension from the
        # actual new journal range, never by subtracting unordered net amounts.
        journal = self.trees["journal"]
        self.roots["journal"] = journal.patch(self.roots["journal"], updates.pop("journal"))
        tree, keys = self.trees["dimensions"], tuple(dimensions)
        old_values = tree.get_many(self.roots["dimensions"], keys)
        for key, old in zip(keys, old_values, strict=True):
            value = old.value if old else None
            if value is not None:
                self._dimension(key, value)
            count = (value[3] if value is not None else 0) + dimensions[key]
            if count < 0:
                fail("storage_corrupt", "财务余额维度的真实分录计数损坏。")
            summary = journal.summary(self.roots["journal"], first=key, last=prefix_end(key))
            if (count == 0 and summary is not None) or (
                count and (summary is None or summary[0] != key)
            ):
                fail("storage_corrupt", "财务余额维度缺少对应的实际分录摘要。")
            updates["dimensions"][key] = (
                Entry(key, [*parts[key], count, summary[1].record()]) if count else None
            )
        for name, changes in updates.items():
            self.roots[name] = self.trees[name].patch(self.roots[name], changes)

    @staticmethod
    def _dimension(key, value):
        if (
            type(value) is not list
            or len(value) != 5
            or type(value[0]) is not str
            or any(part is not None and type(part) is not str for part in value[1:3])
            or type(value[3]) is not int
            or value[3] < 1
            or dimension_key(*value[:3]) != key
        ):
            fail("storage_corrupt", "财务余额维度目录损坏。")
        return tuple(value[:3]), Fold.from_record(value[4])

    def cube(self, opening):
        from ._balances import sum_amounts
        from ._journal_amounts import journal_amount

        result = {}
        for row in opening:
            dim = row["account_code"], row.get("party_id"), row.get("object_id")
            result[dim] = sum_amounts(
                (
                    result.get(dim, 0),
                    journal_amount(row["debit_fen"]),
                    -journal_amount(row["credit_fen"]),
                )
            )
        for entry in self.trees["dimensions"].entries(self.roots["dimensions"]):
            dim, fold = self._dimension(entry.key, entry.value)
            result[dim] = fold.finish(result.get(dim, 0))
        return result

    def resource_heads(self):
        """Read detached selected headers and controlled rows, not whole Source bodies."""
        result = {}
        for entry in self.trees["resources"].entries(self.roots["resources"]):
            value = validate_resource_projection(entry.value)
            head = body_head(value) | {
                "atom": source_atom(value["source"]),
                "resources": value["resources"],
            }
            if chains.selected_key(head) != entry.key or head["result_id"] in result:
                fail("storage_corrupt", "财务受控资源没有对应的正式选中来源。")
            result[head["result_id"]] = head
        return result

    def source_atoms(self):
        return tuple(
            dict.fromkeys(
                entry.value[0] for entry in self.trees["journal"].entries(self.roots["journal"])
            )
        )


class FlowRole:
    def __init__(self, spec, store, bodies, saved=None):
        self.spec, self.bodies = spec, bodies
        self.tree = Tree(
            "financial-flow/" + ("cash-and-profit/1" if spec["cash"] else "profit/1"),
            FlowStrategy(bodies, include_cash=spec["cash"]),
            store,
        )
        if saved is not None and (type(saved) is not dict or set(saved) != {"root", "supplements"}):
            fail("storage_corrupt", "财务发生角色根目录损坏。")
        self.root = self.tree.root(saved["root"] if saved is not None else None)
        self.supplements = _counts(saved["supplements"] if saved is not None else {})

    def wire(self):
        return {"root": _wire(self.root), "supplements": dict(self.supplements)}

    def root_ids(self):
        return (self.root.digest,) if self.root else ()

    def _entries(self, selected, spec):
        bodies = self.bodies.get_many(head["atom"] for head, _, _ in selected)
        return flow_entries(
            tuple(
                (head["atom"], body, sign)
                for (head, sign, _), body in zip(selected, bodies, strict=True)
            ),
            view=spec["view"],
            first=spec["first"],
            last=spec["last"],
            anchors={head["result_id"]: anchor for head, _, anchor in selected if anchor},
            include_cash=self.spec["cash"],
        )

    def patch(self, changed_chains):
        self._replace(
            (
                (
                    chains.flow(old, self.spec["view"], self.spec["first"], self.spec["last"]),
                    chains.flow(new, self.spec["view"], self.spec["first"], self.spec["last"]),
                )
                for old, new in changed_chains
            ),
            self.spec,
        )

    def extend(self, values, previous_spec=None):
        self._replace(
            (
                (
                    chains.flow(
                        chain, previous_spec["view"], previous_spec["first"], previous_spec["last"]
                    )
                    if previous_spec
                    else (),
                    chains.flow(chain, self.spec["view"], self.spec["first"], self.spec["last"]),
                )
                for chain in values
            ),
            previous_spec,
        )

    def _replace(self, pairs, previous_spec):
        updates = {}
        for old, new in pairs:
            if old == new:
                continue
            for entry in self._entries(old, previous_spec or self.spec):
                updates[entry.key] = None
                body = self.bodies.get_many((entry.value[0],))[0]
                _count_body(
                    self.supplements,
                    body,
                    -1,
                    flow_source_used(body, "profit")
                    or (self.spec["cash"] and flow_source_used(body, "cash")),
                )
            for entry in self._entries(new, self.spec):
                updates[entry.key] = entry
                body = self.bodies.get_many((entry.value[0],))[0]
                _count_body(
                    self.supplements,
                    body,
                    1,
                    flow_source_used(body, "profit")
                    or (self.spec["cash"] and flow_source_used(body, "cash")),
                )
        self.root = self.tree.patch(self.root, updates)

    def summary(self):
        return self.tree.summary(self.root)

    def entries(self):
        return self.tree.entries(self.root)
