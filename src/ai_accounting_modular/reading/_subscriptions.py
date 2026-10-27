"""Current Reading input subscriptions, with ordered forward and reverse indexes."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    canonical_json,
    loads_json,
)
from ai_accounting_modular.storage.public import Entry, Tree, TreePlan, TreeStore

from . import _subscription_keys as keys

SCOPE = "_reading/subscriptions"
COMPONENT = "_subscriptions"
CONTROL, TARGETS = "_subscription_control", "_subscription_targets"
NAMESPACE = "_subscription"
NODES, REFS = NAMESPACE + "_nodes", NAMESPACE + "_node_refs"
FORMAT = "reading-input-subscriptions/1"
_EMPTY = ReadInputs()


class _Count:
    def leaf(self, entries):
        return len(entries)

    def branch(self, children):
        return sum(child.count for child in children)

    def encode_summary(self, value):
        return value

    def decode_summary(self, value):
        if type(value) is not int or value < 0:
            keys.corrupt()
        return value

    def summary_from_count(self, count):
        return count


@dataclass(frozen=True)
class SubscriptionPlan:
    """A writer-local proposal; checking expansion does not write any rows."""

    expanded: bool
    blocks: TreePlan
    control: str | None
    target_key: str
    target_json: str | None
    base_control: str | None


class Directory:
    def __init__(self, repository):
        self.repository, self._session = repository, repository._session
        row = self._session.fetchone(
            "SELECT * FROM read_item WHERE scope_key=? AND collection_key=? AND item_id='head'",
            (SCOPE, CONTROL),
        )
        self.token, self.revision, descriptor = None, 0, None
        if row is None:
            if self._session.fetchone(
                "SELECT 1 FROM read_item WHERE scope_key=? LIMIT 1", (SCOPE,)
            ):
                keys.corrupt()
            # An empty new/fully reset Reading has no published consumers.
            # This exceptional initialization probe is never a normal route scan.
            if self._session.fetchone(
                "SELECT 1 FROM read_component WHERE CASE WHEN json_valid(scope_key) "
                "THEN json_extract(scope_key,'$.page') IN ('monthly','quarterly_report') "
                "ELSE 0 END LIMIT 1"
            ):
                keys.corrupt()
        else:
            try:
                self.token = row["payload_json"]
                value = loads_json(self.token)
                if (
                    type(value) is not dict
                    or set(value) != {"format", "revision", "root"}
                    or value["format"] != FORMAT
                    or type(value["revision"]) is not int
                    or value["revision"] < 1
                    or canonical_json(value) != self.token
                ):
                    keys.corrupt()
                self.revision, descriptor = value["revision"], value["root"]
                self._check_row(row, CONTROL, "head", exact=True)
                if descriptor is None and self._session.fetchone(
                    "SELECT 1 FROM read_item WHERE scope_key=? AND collection_key=? LIMIT 1",
                    (SCOPE, NODES),
                ):
                    keys.corrupt()
            except (ContractError, KeyError, TypeError, ValueError) as error:
                raise ContractError("storage_corrupt", "读取订阅的当前根不完整。") from error
        self.store = TreeStore(self, namespace=NAMESPACE)
        self.reverse = Tree(FORMAT + "/reverse", _Count(), self.store)
        self.forward = Tree(FORMAT + "/forward", _Count(), self.store)
        self.root = self.reverse.root(descriptor)
        self._targets, self._periods = {}, {}

    def _check_row(self, row, collection, identity, *, exact=False):
        if (
            row["scope_key"] != SCOPE
            or row["component_key"] != COMPONENT
            or row["collection_key"] != collection
            or row["item_id"] != identity
            or row["sort_key"] != identity
            or type(row["head"]) is not int
            or (row["head"] != self.revision if exact else not 1 <= row["head"] <= self.revision)
            or any(
                row[name] is not None
                for name in ("business_id", "object_id", "group_key", "category")
            )
        ):
            keys.corrupt()

    def get_blocks(self, requested):
        requested = tuple(requested)
        if not requested:
            return ()
        if any(collection not in {NODES, REFS} for collection, _ in requested):
            keys.corrupt()
        rows = self._session.fetchall(
            "SELECT * FROM read_item WHERE scope_key=? AND (collection_key,item_id) IN "
            "(SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?))",
            (SCOPE, canonical_json([list(value) for value in requested])),
        )
        found = {}
        for row in rows:
            identity = row["collection_key"], row["item_id"]
            self._check_row(row, *identity)
            found[identity] = row["payload_json"]
        return tuple(found.get(identity) for identity in requested)

    def _target(self, scope_key, component):
        identity = keys.key(scope_key, component)
        if identity not in self._targets:
            row = self._session.fetchone(
                "SELECT * FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
                (SCOPE, TARGETS, identity),
            )
            root = None
            if row is not None:
                try:
                    self._check_row(row, TARGETS, identity)
                    value = loads_json(row["payload_json"])
                    if (
                        type(value) is not dict
                        or set(value) != {"target", "root"}
                        or value["target"] != [scope_key, component]
                        or canonical_json(value) != row["payload_json"]
                    ):
                        keys.corrupt()
                    root = self.forward.root(value["root"])
                except (ContractError, KeyError, TypeError, ValueError) as error:
                    raise ContractError("storage_corrupt", "读取目标的订阅归属不完整。") from error
            self._targets[identity] = row is not None, root
        return self._targets[identity]

    def known(self, scope_key, component):
        return self._target(scope_key, component)[0]

    @staticmethod
    def _selector(entry):
        value = entry.value
        if (
            type(value) is not list
            or len(value) != 2
            or type(value[0]) is not str
            or value[0] not in keys.FIELDS | keys.SPECIAL
            or type(value[1]) is not str
            or (value[0] in keys.FLAGS | keys.SPECIAL and value[1] != "")
            or (value[0] not in keys.FLAGS | keys.SPECIAL and not value[1])
            or entry.key != keys.key(*value)
        ):
            keys.corrupt()
        return tuple(value)

    def _month(self, scope_key, component):
        identity = scope_key, component
        if identity not in self._periods:
            self._periods[identity] = keys.target(*identity)
        return self._periods[identity]

    def _reverse_key(self, scope_key, component, selector):
        return keys.key(*selector, self._month(scope_key, component), scope_key, component)

    def candidates(self, impact):
        """Read only the selector ranges named by a real change, then prove ownership."""
        candidates = set()
        for field, identity, first, last, component_filter in keys.queries(impact):
            end = (
                keys.key(field, identity, last)
                if last is not None
                else keys.bounds(field, identity)[1]
            )
            for entry in self.reverse.entries(
                self.root, first=keys.key(field, identity, first), last=end
            ):
                value = entry.value
                if (
                    type(value) is not list
                    or len(value) != 2
                    or any(type(part) is not str for part in value)
                ):
                    keys.corrupt()
                scope_key, component = value
                selector = field, identity
                if entry.key != self._reverse_key(scope_key, component, selector):
                    keys.corrupt()
                known, root = self._target(scope_key, component)
                owned = self.forward.get_many(root, (keys.key(*selector),))[0]
                if not known or owned is None or self._selector(owned) != selector:
                    keys.corrupt()
                if component_filter is None or component == component_filter:
                    candidates.add((scope_key, component))
        return tuple(sorted(candidates))

    def plan(
        self,
        scope_key,
        component,
        *,
        inputs=None,
        adds=_EMPTY,
        removes=_EMPTY,
        base_target=None,
    ):
        """Build from the current writer root; no cross-task root token is accepted."""
        month = self._month(scope_key, component)
        known, old_forward = self._target(scope_key, component)
        automatic = keys.automatic(component, month)
        if base_target is not None:
            if known or inputs is not None:
                raise TypeError("only an unpublished target can inherit subscriptions")
            base_known, inherited_root = self._target(*base_target)
            if not base_known:
                keys.corrupt()
            inherited = {
                self._selector(entry) for entry in self.forward.entries(inherited_root)
            } - keys.automatic(base_target[1], self._month(*base_target))
            removed = keys.selectors(removes)
            if not removed <= inherited:
                keys.corrupt()
            added = (inherited - removed) | keys.selectors(adds) | automatic
            removed = frozenset()
        elif inputs is not None:
            if adds != _EMPTY or removes != _EMPTY:
                raise TypeError("complete subscriptions cannot also carry a delta")
            previous = {self._selector(entry) for entry in self.forward.entries(old_forward)}
            proposed = keys.selectors(inputs) | automatic
            added, removed = proposed - previous, previous - proposed
        else:
            added, removed = keys.selectors(adds), keys.selectors(removes)
            if added & removed:
                raise TypeError("a subscription cannot be added and removed together")
            actual = self.forward.get_many(
                old_forward, tuple(keys.key(*value) for value in added | removed | automatic)
            )
            previous = {self._selector(entry) for entry in actual if entry is not None}
            if not removed <= previous:
                keys.corrupt()
            added = (added | automatic) - previous
        touched = added | removed
        reverse_keys = {
            selector: self._reverse_key(scope_key, component, selector) for selector in touched
        }
        reverse_values = self.reverse.get_many(self.root, tuple(reverse_keys.values()))
        for selector, actual in zip(reverse_keys, reverse_values, strict=True):
            if (
                selector in removed and (actual is None or actual.value != [scope_key, component])
            ) or (selector in added and actual is not None):
                keys.corrupt()
        if not touched and known:
            return SubscriptionPlan(
                False, TreePlan((), ()), None, keys.key(scope_key, component), None, self.token
            )
        forward_changes = {keys.key(*selector): None for selector in removed}
        forward_changes.update(
            (keys.key(*selector), Entry(keys.key(*selector), list(selector))) for selector in added
        )
        reverse_changes = {reverse_keys[selector]: None for selector in removed}
        reverse_changes.update(
            (reverse_keys[selector], Entry(reverse_keys[selector], [scope_key, component]))
            for selector in added
        )
        new_forward = self.forward.patch(old_forward, forward_changes)
        new_reverse = self.reverse.patch(self.root, reverse_changes)
        blocks = self.store.finish(
            tuple(root.digest for root in (self.root, old_forward) if root is not None),
            tuple(root.digest for root in (new_reverse, new_forward) if root is not None),
        )
        control = canonical_json(
            {
                "format": FORMAT,
                "revision": self.revision + 1,
                "root": new_reverse.wire() if new_reverse else None,
            }
        )
        target_json = canonical_json(
            {"target": [scope_key, component], "root": new_forward.wire() if new_forward else None}
        )
        return SubscriptionPlan(
            bool(added), blocks, control, keys.key(scope_key, component), target_json, self.token
        )

    def commit(self, plan):
        """Publish a checked writer-local proposal after its component's CAS succeeds."""
        if not isinstance(plan, SubscriptionPlan):
            raise TypeError("subscription publication requires its prepared plan")
        current = self._session.fetchone(
            "SELECT payload_json FROM read_item WHERE scope_key=? AND collection_key=? "
            "AND item_id='head'",
            (SCOPE, CONTROL),
        )
        if (current[0] if current else None) != plan.base_control:
            return False
        if plan.control is None:
            return True
        revision = loads_json(plan.control)["revision"]
        self._session.executemany(
            "DELETE FROM read_item WHERE scope_key=? AND collection_key=? AND item_id=?",
            ((SCOPE, *identity) for identity in plan.blocks.deletes),
        )
        values = [(write.collection, write.key, write.payload_json) for write in plan.blocks.writes]
        values.extend(
            ((TARGETS, plan.target_key, plan.target_json), (CONTROL, "head", plan.control))
        )
        self._session.executemany(
            "INSERT INTO read_item(scope_key,collection_key,item_id,component_key,head,sort_key,"
            "payload_json) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope_key,collection_key,item_id) DO UPDATE SET "
            "component_key=excluded.component_key,head=excluded.head,sort_key=excluded.sort_key,"
            "business_id=NULL,object_id=NULL,group_key=NULL,category=NULL,payload_json=excluded.payload_json",
            (
                (SCOPE, collection, identity, COMPONENT, revision, identity, raw)
                for collection, identity, raw in values
            ),
        )
        self.__init__(self.repository)
        return True
