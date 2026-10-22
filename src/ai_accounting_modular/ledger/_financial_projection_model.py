"""Company-owned financial contributions and exact range roots, atomically publishable."""

from hashlib import sha256

from ai_accounting_modular.contracts.public import (
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _financial_projection_bodies as bodies_module
from . import _financial_projection_chains as chains
from ._balance_cube import previous
from ._closing import closed_report
from ._financial_cells import Issues
from ._financial_projection_bodies import source_atom
from ._financial_projection_indexes import BodyDirectory, PlainStrategy
from ._financial_projection_requests import request
from ._financial_projection_roles import FlowRole, StockRole
from ._financial_projection_types import (
    FINANCIAL_PROJECTION_FORMAT,
    FinancialProjectionPlan,
    FinancialProjectionRequest,
)
from ._financial_selection import Selection
from ._financial_tree import Entry, Tree
from ._financial_tree_store import TreeStore
from ._planning import source_digest
from ._values import fail


class EmptyReader:
    header_json = None

    def get_blocks(self, keys):
        return (None,) * len(keys)


def verified_request(value, read):
    if not isinstance(value, FinancialProjectionRequest):
        fail("invalid_input", "财务投影须使用明确的正式读取请求。")
    actual = request(value.kind, value.scope, read)
    if actual != value:
        fail("invalid_input", "财务投影请求的范围、身份或定位不一致。")
    return actual


def _root(root):
    return root.wire() if root else None


def _role_spec(spec):
    if type(spec) is not dict or spec.get("view") not in ("current", "as_posted"):
        fail("storage_corrupt", "财务角色没有明确的正式读取口径。")
    if spec.get("kind") == "stock" and set(spec) == {"kind", "view", "cutoff"}:
        validate_contract("core.schema.json", "Month", spec["cutoff"])
    elif (
        spec.get("kind") == "flow"
        and set(spec) == {"kind", "view", "first", "last", "cash"}
        and type(spec["cash"]) is bool
    ):
        for name in ("first", "last"):
            validate_contract("core.schema.json", "Month", spec[name])
        if spec["first"] > spec["last"]:
            fail("storage_corrupt", "财务发生角色的月份范围损坏。")
    else:
        fail("storage_corrupt", "财务角色没有受控的领域定义。")
    return spec


def required_roles(value, start, *, saved):
    scope = value.scope
    first, last, view = scope["period_from"], scope["period_to"], scope["view"]
    if value.kind == "snapshot" and saved:
        return ()
    result = [{"kind": "stock", "view": view, "cutoff": last}]
    if value.kind == "summary_values":
        if not saved:
            result.append(
                {"kind": "flow", "view": view, "first": first, "last": last, "cash": False}
            )
    else:
        year = last[:4] + "-01"
        result.extend(
            {"kind": "stock", "view": view, "cutoff": cutoff}
            for cutoff in (previous(year), previous(first))
            if cutoff >= start
        )
        result.extend(
            {"kind": "flow", "view": view, "first": begin, "last": last, "cash": True}
            for begin in (first, year)
        )
    return tuple({content_digest(spec): spec for spec in result}.values())


class Model:
    def __init__(self, read, base=None):
        self.read, self.base = read, base if base is not None else EmptyReader()
        self.store = TreeStore(self.base)
        raw = self.base.header_json
        if raw is None:
            header = {
                "format": FINANCIAL_PROJECTION_FORMAT,
                "company_id": read.company_id,
                "database_id": read.database_id,
                "start_period": read.start_period,
                "bodies": None,
                "chains": None,
                "roles": {},
                "requests": {},
            }
        else:
            header = loads_json(raw)
            if (
                type(header) is not dict
                or set(header)
                != {
                    "format",
                    "company_id",
                    "database_id",
                    "start_period",
                    "bodies",
                    "chains",
                    "roles",
                    "requests",
                }
                or type(header["format"]) is not int
                or header["format"] != FINANCIAL_PROJECTION_FORMAT
                or (header["company_id"], header["database_id"], header["start_period"])
                != (read.company_id, read.database_id, read.start_period)
                or type(header["roles"]) is not dict
                or type(header["requests"]) is not dict
                or canonical_json(header) != raw
            ):
                fail("storage_corrupt", "财务派生模型的公司、格式或范围不一致。")
        self.header = header
        self.bodies = BodyDirectory(self.store, header["bodies"])
        self.chain_tree = Tree("financial-adoption-chains/1", PlainStrategy(), self.store)
        self.chain_root = self.chain_tree.root(header["chains"])
        if bool(self.bodies.root) != bool(self.chain_root) or (
            self.bodies.root and self.bodies.root.count < self.chain_root.count
        ):
            fail("storage_corrupt", "财务来源正文与正式采用链目录不完整。")
        self.roles, self.requests = {}, {}
        for key, saved in header["roles"].items():
            if (
                type(saved) is not dict
                or set(saved) != {"spec", "data"}
                or type(saved["data"]) is not dict
            ):
                fail("storage_corrupt", "财务角色保存目录损坏。")
            spec = _role_spec(saved["spec"])
            if content_digest(spec) != key:
                fail("storage_corrupt", "财务角色定位与范围不一致。")
        for key, spec in header["requests"].items():
            if type(spec) is not dict or set(spec) != {"kind", "scope"}:
                fail("storage_corrupt", "财务读取请求保存目录损坏。")
            actual = request(spec["kind"], spec["scope"], read)
            if actual.key != key:
                fail("storage_corrupt", "财务读取请求与实际公司范围不一致。")
            self.requests[key] = actual
        self.empty_stock = StockRole(
            {"kind": "stock", "view": "current", "cutoff": previous(read.start_period)},
            self.store,
            self.bodies,
        )
        self._saved = {}

    def _role(self, spec, saved=None):
        cls = StockRole if spec["kind"] == "stock" else FlowRole
        return cls(spec, self.store, self.bodies, saved)

    def root_ids(self):
        # Publication owns the complete graph. A consumer instead resolves only
        # the roles declared by its request, within this same read snapshot.
        for saved in self.header["roles"].values():
            self._get_role(saved["spec"])
        roots = [root.digest for root in (self.bodies.root, self.chain_root) if root]
        roots.extend(identity for role in self.roles.values() for identity in role.root_ids())
        return tuple(roots)

    def stock_role(self, view, cutoff):
        return self._get_role({"kind": "stock", "view": view, "cutoff": cutoff})

    def flow_role(self, view, first, last, *, include_cash):
        return self._get_role(
            {"kind": "flow", "view": view, "first": first, "last": last, "cash": include_cash}
        )

    def _get_role(self, spec):
        key = content_digest(spec)
        if key not in self.roles and key in self.header["roles"]:
            saved = self.header["roles"][key]
            self.roles[key] = self._role(saved["spec"], saved["data"])
        if key not in self.roles or self.roles[key].spec != spec:
            fail("storage_corrupt", "财务请求缺少实际覆盖的正式角色。")
        return key, self.roles[key]

    @staticmethod
    def _role_base(spec, available):
        candidates = [
            (key, previous)
            for key, previous in available.items()
            if previous["kind"] == spec["kind"]
            and previous["view"] == spec["view"]
            and (
                spec["kind"] == "stock"
                or (previous["first"] == spec["first"] and previous["cash"] == spec["cash"])
            )
        ]
        field = "cutoff" if spec["kind"] == "stock" else "last"
        before = [value for value in candidates if value[1][field] <= spec[field]]
        return (
            max(before, key=lambda value: value[1][field])
            if before
            else (min(candidates, key=lambda value: value[1][field]) if candidates else None)
        )

    def _role_plan(self, requested, *, discover):
        available = {key: role.spec for key, role in self.roles.items()}
        requests = dict(self.requests)
        requests.update((value.key, value) for value in requested)
        result = []
        for value in requests.values():
            for spec in required_roles(value, self.read.start_period, saved=self.saved(value)):
                key = content_digest(spec)
                if key in available:
                    continue
                if not discover:
                    fail("storage_corrupt", "已有财务读取请求丢失了正式角色覆盖。")
                previous = self._role_base(spec, available)
                result.append((key, spec, previous))
                available[key] = spec
        return result

    def saved(self, value):
        if value.key not in self._saved:
            repo = self.read.repository("ledger")
            closed = value.scope["view"] == "as_posted" and repo.is_closed(
                value.scope["period_to"], start_period=self.read.start_period
            )
            if closed and value.kind == "snapshot":
                header = repo.close_header(value.scope["period_to"])
                closed = value.scope["period_from"] == value.scope["period_to"] == header["period"]
            self._saved[value.key] = closed_report(value.scope, self.read) if closed else None
        return self._saved[value.key]

    def _old_chains(self, identities=None):
        if identities is None:
            values = tuple(self.chain_tree.entries(self.chain_root))
        else:
            values = tuple(
                entry
                for entry in self.chain_tree.get_many(
                    self.chain_root, tuple(chains.key(*identity) for identity in identities)
                )
                if entry is not None
            )
        result = {}
        for entry in values:
            value = entry.value
            if (
                type(value) is not list
                or len(value) != 3
                or any(type(part) is not str or not part for part in value[:2])
                or chains.key(*value[:2]) != entry.key
            ):
                fail("storage_corrupt", "财务正式链的定位与正文不一致。")
            identity = tuple(value[:2])
            result[identity] = chains.heads(identity, value[2])
        return result

    def advance(self, requested, changes):
        requested = tuple(verified_request(value, self.read) for value in requested)
        old_roots = self.root_ids()
        fresh = any(value.key not in self.requests for value in requested)
        cold = self.base.header_json is None
        planned = self._role_plan(requested, discover=cold or fresh)
        identities = {(value.business_id, value.result_slot) for value in changes}
        repo = self.read.repository("ledger")
        if not cold:
            for _, spec, previous in planned:
                identities.update(
                    repo.financial_projection_candidates(spec, previous[1] if previous else None)
                )
        identities = None if cold else tuple(sorted(identities))
        old = self._old_chains(identities)
        actual = repo.financial_projection_chains(identities)
        capture = Selection(
            self.read.repository("ledger"),
            "current",
            self.read.start_period,
            self.read.start_period,
            Issues(),
        )
        prior = {head["result_id"]: head for values in old.values() for head in values}
        signatures, atoms = {}, {}
        for head in actual:
            identity = head["result_id"]
            signature = source_digest(
                *(
                    head[key]
                    for key in (
                        "calculation_key",
                        "calculation_revision",
                        "basis_digest",
                        "target_digest",
                    )
                )
            )
            signatures[identity] = signature
            previous = prior.get(identity)
            if (
                previous
                and type(head["generation"]) is int
                and head["generation"] == previous["generation"]
            ):
                if signature != previous["signature"]:
                    fail("storage_corrupt", "同一正式代际的来源签名发生了未登记改变。")
                atoms[identity] = previous["atom"]
            else:
                atoms[identity] = source_atom(capture.source(head))
        records = chains.records(actual, atoms, signatures)
        new = {identity: chains.heads(identity, values) for identity, values in records.items()}
        changed = [
            (identity, old.get(identity, ()), new.get(identity, ()))
            for identity in sorted(old.keys() | new.keys())
            if old.get(identity, ()) != new.get(identity, ())
        ]
        old_atoms = {head["atom"] for _, before, _ in changed for head in before}
        new_atoms = {head["atom"] for _, _, after in changed for head in after}
        removed = old_atoms - new_atoms
        # Old Counter legs can still be needed while replacing role leaves in this plan.
        self.bodies.get_many(sorted(removed))
        known = {head["atom"] for heads in old.values() for head in heads}
        missing = [head for head in actual if atoms[head["result_id"]] not in known]
        captured = bodies_module.capture_bodies(capture, missing) if missing else {}
        self.bodies._bodies.update(captured)
        writes = {identity: Entry(identity, body) for identity, body in captured.items()}
        writes.update((identity, None) for identity in removed)
        self.bodies.root = self.bodies.tree.patch(self.bodies.root, writes)
        updates = {}
        for identity, _, after in changed:
            key = chains.key(*identity)
            updates[key] = Entry(key, [*identity, records[identity]]) if after else None
        self.chain_root = self.chain_tree.patch(self.chain_root, updates)
        pairs = tuple((before, after) for _, before, after in changed)
        for role in self.roles.values():
            role.patch(pairs)
        self.requests.update((value.key, value) for value in requested)
        for key, spec, previous in planned:
            base = self.roles[previous[0]] if previous else None
            role = self._role(spec, base.wire() if base else None)
            role.extend(tuple(new.values()), base.spec if base else None)
            self.roles[key] = role
        required = {
            content_digest(spec)
            for value in self.requests.values()
            for spec in required_roles(value, self.read.start_period, saved=self.saved(value))
        }
        self.roles = {key: role for key, role in self.roles.items() if key in required}
        self.header.update(
            bodies=_root(self.bodies.root),
            chains=_root(self.chain_root),
            roles={
                key: {"spec": role.spec, "data": role.wire()} for key, role in self.roles.items()
            },
            requests={
                key: {"kind": value.kind, "scope": value.scope}
                for key, value in self.requests.items()
            },
        )
        return old_roots

    def descriptor(self, value):
        boundary = Selection(
            self.read.repository("ledger"),
            value.scope["view"],
            value.scope["period_to"],
            self.read.start_period,
            Issues(),
        )
        saved = self.saved(value)
        last = value.scope["period_to"]
        proof = {
            "contract": "ledger-financial-selection/2",
            "request": value.key,
            "roles": {
                content_digest(spec): self._get_role(spec)[1].wire()
                for spec in required_roles(value, self.read.start_period, saved=saved)
            },
            "boundaries": [
                source_atom(boundary.source(head))
                for head in (boundary.opening, boundary.continuation)
                if head
            ],
            "closed_periods": self.read.repository("ledger").closed_periods(
                last[:4] + "-01", last, start_period=self.read.start_period
            ),
            "saved_selection": saved["selection"]["selection_digest"] if saved else None,
        }
        version = (
            saved["selection"]["selection_digest"]
            if saved and value.kind == "snapshot"
            else content_digest(proof)
        )
        return {
            "format": FINANCIAL_PROJECTION_FORMAT,
            "request": value.key,
            "kind": value.kind,
            "scope": value.scope,
            "proof": proof,
            "version": version,
        }

    def plan(self, old_roots):
        plan = self.store.finish(old_roots, self.root_ids())
        writes = [(value.collection, value.key, value.payload_json) for value in plan.writes]
        for value in self.requests.values():
            raw = canonical_json(self.descriptor(value))
            if self.base.get_blocks((value.descriptor_key,))[0] != raw:
                writes.append((*value.descriptor_key, raw))
        raw = canonical_json(self.header)
        return FinancialProjectionPlan(
            raw, sha256(raw.encode("utf-8")).hexdigest(), tuple(writes), plan.deletes
        )

    def supplements(self, selection, first, last):
        result = set()
        for key in selection.used_roles:
            result.update(
                period for period in self.roles[key].supplements if first <= period <= last
            )
        for body in self.bodies.get_many(sorted(selection.used_atoms)):
            if body["baseline_result_id"] and first <= body["posting_period"] <= last:
                result.add(body["posting_period"])
        for identity in selection.used:
            head = selection.heads[identity]
            if head.get("baseline_result_id") and first <= head["posting_period"] <= last:
                result.add(head["posting_period"])
        return sorted(result)
