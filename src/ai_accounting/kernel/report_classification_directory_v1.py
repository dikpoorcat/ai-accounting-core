"""Fixed released-v1 reader for the frozen classification voucher-key root.

This file is intentionally independent of later current directory rules.
Historical verification calls its read-only comparison under the v1 content
context and never repairs or writes old sources.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .contracts import KernelError
from .history_encoding_v1 import canonical
from .key_membership_filter_v1 import build_keys_filter, decode_keys_filter, may_contain

DERIVED_ROOT_NAME = "report_classifications"
FORMAT_VERSION = 2
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class PreparedClassificationDirectory:
    period: int
    close_digest: bytes
    content: str
    root_digest: bytes
    nodes: tuple[tuple[bytes, str], ...]


def _invalid(period, reason):
    raise KernelError(
        "content_integrity_failed",
        "冻结报表分类目录与权威来源不一致",
        component="report_classifications",
        record_id=str(period),
        reason=reason,
    )


def _sha(content):
    return hashlib.sha256(content.encode("utf-8")).digest()


def _key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _hex_digest(value):
    return type(value) is str and _HEX_DIGEST.fullmatch(value) is not None


class _Nodes:
    def __init__(self, connection, *, overlay=None):
        self.connection = connection
        self.overlay = {} if overlay is None else overlay
        self.loaded = {}
        self.parsed = {}

    def write(self, value):
        content = canonical(value)
        checksum = _sha(content)
        self.overlay[checksum] = content
        self.parsed[checksum] = value
        return checksum

    def read(self, checksum, period):
        if type(checksum) is not bytes or len(checksum) != 32:
            _invalid(period, "node_digest_invalid")
        if checksum in self.parsed:
            return self.parsed[checksum]
        content = self.overlay.get(checksum, self.loaded.get(checksum))
        if content is None:
            row = self.connection.execute(
                "SELECT content FROM report_classification_node WHERE digest=?", (checksum,)
            ).fetchone()
            if row is None:
                _invalid(period, "node_missing")
            content = row["content"]
        if _sha(content) != checksum:
            _invalid(period, "node_digest_mismatch")
        try:
            value = json.loads(content)
            if canonical(value) != content or not isinstance(value, dict):
                _invalid(period, "node_shape_invalid")
            kind = value.get("kind")
            if kind == "leaf":
                entries = value.get("entries")
                if (
                    type(value.get("key")) is not str
                    or not value["key"]
                    or value.get("hash") != _key_hash(value["key"])
                    or not isinstance(entries, list)
                    or not entries
                    or any(
                        not isinstance(entry, list)
                        or len(entry) != 4
                        or type(entry[0]) is not str
                        or not entry[0]
                        or not _hex_digest(entry[1])
                        or type(entry[2]) is not int
                        or type(entry[3]) is not bool
                        for entry in entries
                    )
                    or len({entry[0] for entry in entries}) != len(entries)
                    or entries != sorted(entries)
                    or value.get("keys") != 1
                    or value.get("conflicts") != int(len(entries) > 1)
                    or value.get("unsafe") != sum(not item[3] for item in entries)
                ):
                    _invalid(period, "leaf_shape_invalid")
            elif kind == "branch":
                children = value.get("children")
                if (
                    not isinstance(children, dict)
                    or not children
                    or any(
                        type(part) is not str
                        or len(part) != 1
                        or part not in "0123456789abcdef"
                        or not _hex_digest(child)
                        for part, child in children.items()
                    )
                    or type(value.get("keys")) is not int
                    or value["keys"] < 2
                    or type(value.get("conflicts")) is not int
                    or not 0 <= value["conflicts"] <= value["keys"]
                    or type(value.get("unsafe")) is not int
                    or value["unsafe"] < 0
                ):
                    _invalid(period, "branch_shape_invalid")
            else:
                _invalid(period, "node_kind_invalid")
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise KernelError(
                "content_integrity_failed", "冻结报表分类目录节点格式错误"
            ) from exc
        self.parsed[checksum] = value
        return value

    def load_many(self, checksums, period):
        missing = [
            checksum
            for checksum in set(checksums)
            if checksum not in self.parsed
            and checksum not in self.overlay
            and checksum not in self.loaded
        ]
        for start in range(0, len(missing), 400):
            batch = missing[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in self.connection.execute(
                "SELECT digest,content FROM report_classification_node "
                f"WHERE digest IN ({placeholders})",
                batch,
            ):
                self.loaded[row["digest"]] = row["content"]
            for checksum in batch:
                if checksum not in self.loaded:
                    _invalid(period, "node_missing")
                self.read(checksum, period)


def _leaf(key, entries):
    ordered = sorted(entries)
    return {
        "kind": "leaf",
        "key": key,
        "hash": _key_hash(key),
        "entries": ordered,
        "keys": 1,
        "conflicts": int(len(ordered) > 1),
        "unsafe": sum(not item[3] for item in ordered),
    }


def _branch(nodes, children, period):
    values = [nodes.read(bytes.fromhex(child), period) for child in children.values()]
    return {
        "kind": "branch",
        "children": dict(sorted(children.items())),
        "keys": sum(item["keys"] for item in values),
        "conflicts": sum(item["conflicts"] for item in values),
        "unsafe": sum(item["unsafe"] for item in values),
    }


def _insert(nodes, checksum, key, entry, depth, period):
    hashed = _key_hash(key)
    if checksum is None:
        return nodes.write(_leaf(key, [entry]))
    node = nodes.read(checksum, period)
    if node["kind"] == "leaf":
        if node["key"] == key:
            existing = {item[0]: item for item in node["entries"]}
            if entry[0] in existing:
                if existing[entry[0]][1] != entry[1]:
                    _invalid(period, "adopted_revision_changed")
                return checksum
            return nodes.write(_leaf(key, [*node["entries"], entry]))
        old_hash = node["hash"]
        if depth >= 64 or old_hash == hashed:
            _invalid(period, "voucher_key_hash_collision")
        old_part, new_part = old_hash[depth], hashed[depth]
        if old_part == new_part:
            child = _insert(nodes, checksum, key, entry, depth + 1, period)
            return nodes.write(_branch(nodes, {old_part: child.hex()}, period))
        new_child = nodes.write(_leaf(key, [entry]))
        return nodes.write(
            _branch(nodes, {old_part: checksum.hex(), new_part: new_child.hex()}, period)
        )
    if depth >= 64:
        _invalid(period, "branch_depth_invalid")
    children = dict(node["children"])
    part = hashed[depth]
    previous = bytes.fromhex(children[part]) if part in children else None
    child = _insert(nodes, previous, key, entry, depth + 1, period)
    if child == previous:
        return checksum
    children[part] = child.hex()
    return nodes.write(_branch(nodes, children, period))


def _tree_keys(nodes, tree, period):
    """Read the previous authenticated tree once when preparing a new close."""
    keys = set()
    pending = [tree] if tree is not None else []
    while pending:
        nodes.load_many(pending, period)
        following = []
        for checksum in pending:
            node = nodes.read(checksum, period)
            if node["kind"] == "leaf":
                if node["key"] in keys:
                    _invalid(period, "tree_key_repeated")
                keys.add(node["key"])
            else:
                following.extend(bytes.fromhex(child) for child in node["children"].values())
        pending = following
    return keys


def _root_content(period, close_digest, tree, nodes, keys):
    node = nodes.read(tree, period) if tree is not None else None
    if len(keys) != (node["keys"] if node is not None else 0):
        _invalid(period, "filter_key_count_mismatch")
    return canonical(
        {
            "format": FORMAT_VERSION,
            "period": period,
            "close_digest": close_digest.hex(),
            "tree": tree.hex() if tree is not None else None,
            "keys": node["keys"] if node is not None else 0,
            "conflicts": node["conflicts"] if node is not None else 0,
            "unsafe": node["unsafe"] if node is not None else 0,
            "key_filter": build_keys_filter(keys),
        }
    )


def _reachable_new_nodes(nodes, tree, period):
    """Keep only new nodes used by this finished root, not insertion intermediates."""
    found = {}
    stack = [tree] if tree is not None else []
    while stack:
        checksum = stack.pop()
        if checksum not in nodes.overlay or checksum in found:
            continue
        found[checksum] = nodes.overlay[checksum]
        node = nodes.read(checksum, period)
        if node["kind"] == "branch":
            stack.extend(bytes.fromhex(child) for child in node["children"].values())
    return found


def _read_root(connection, close, *, nodes=None):
    from .content_history_context import close_reader

    period = close["period"]
    reader = close_reader()
    header = reader.verified_header(connection, close, require_marker=True)
    bound = reader.derived_root(header, DERIVED_ROOT_NAME)
    row = connection.execute(
        "SELECT close_digest,content,root_digest FROM report_classification_directory "
        "WHERE period=?",
        (period,),
    ).fetchone()
    if bound is None or row is None:
        _invalid(period, "root_missing")
    if row["close_digest"] != close["digest"] or row["root_digest"] != bound:
        _invalid(period, "root_binding_mismatch")
    content = row["content"]
    if _sha(content) != bound:
        _invalid(period, "root_digest_mismatch")
    try:
        root = json.loads(content)
        tree_hex = root.get("tree")
        if (
            canonical(root) != content
            or root.get("format") != FORMAT_VERSION
            or root.get("period") != period
            or root.get("close_digest") != close["digest"].hex()
            or (tree_hex is not None and not _hex_digest(tree_hex))
            or type(root.get("keys")) is not int
            or type(root.get("conflicts")) is not int
            or type(root.get("unsafe")) is not int
            or not 0 <= root["conflicts"] <= root["keys"]
            or root["unsafe"] < 0
            or (tree_hex is None and (root["keys"] or root["conflicts"] or root["unsafe"]))
        ):
            _invalid(period, "root_shape_invalid")
        if decode_keys_filter(root.get("key_filter")).key_count != root["keys"]:
            _invalid(period, "filter_key_count_mismatch")
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise KernelError(
            "content_integrity_failed", "冻结报表分类目录根格式错误"
        ) from exc
    if tree_hex is not None:
        node = (nodes or _Nodes(connection)).read(bytes.fromhex(tree_hex), period)
        if (
            node["keys"] != root["keys"]
            or node["conflicts"] != root["conflicts"]
            or node["unsafe"] != root["unsafe"]
        ):
            _invalid(period, "root_count_mismatch")
    return root


def _adopted_classifications(
    connection, manifest, period, flow_content, *, allow_absent_financial_reports=False
):
    readiness = manifest.get("readiness")
    financial = readiness.get("financial_reports") if isinstance(readiness, dict) else None
    identifiers = financial.get("facts") if isinstance(financial, dict) else None
    try:
        flow = json.loads(flow_content)
        clean = not flow["issues"]
        certified = dict(flow["classification_refs"]) if clean else {}
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise KernelError(
            "content_integrity_failed", "冻结报表分类目录缺少可核对的月度证明"
        ) from exc
    if financial is None and allow_absent_financial_reports:
        if flow["classification_refs"] or connection.execute(
            "SELECT 1 FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='report_classification' AND f.period<=? LIMIT 1",
            (period,),
        ).fetchone():
            _invalid(period, "absent_financial_reports_has_classifications")
        identifiers = []
    if not isinstance(identifiers, list) or any(type(item) is not str for item in identifiers):
        _invalid(period, "readiness_classifications_invalid")
    if not identifiers:
        return ()
    result = []
    for row in connection.execute(
        "SELECT f.id,f.digest,c.voucher_version_id FROM json_each(?) ids "
        "CROSS JOIN fact_revision f ON f.id=ids.value "
        "JOIN subject s ON s.id=f.subject_id AND s.kind='report_classification' "
        "LEFT JOIN fact_report_classification c ON c.revision_id=f.id",
        (canonical(identifiers),),
    ):
        if not row["voucher_version_id"]:
            _invalid(period, "classification_header_missing")
        ident, digest_hex = row["id"], row["digest"].hex()
        result.append(
            (
                row["voucher_version_id"],
                [ident, digest_hex, period, certified.get(ident) == digest_hex],
            )
        )
    return tuple(sorted(result))


def prepare_classification_directory(
    connection,
    period,
    close_digest,
    manifest,
    flow_content,
    *,
    previous=None,
    nodes=None,
    collect_nodes=True,
    allow_absent_financial_reports=False,
    _previous_keys=None,
):
    """Incrementally copy only paths affected by this close's adopted revisions."""
    nodes = nodes or _Nodes(connection)
    if previous is None:
        prior = connection.execute(
            "SELECT * FROM period_close WHERE period<? ORDER BY period DESC LIMIT 1", (period,)
        ).fetchone()
        previous = _read_root(connection, prior, nodes=nodes) if prior is not None else None
    tree = bytes.fromhex(previous["tree"]) if previous and previous["tree"] else None
    keys = _tree_keys(nodes, tree, period) if _previous_keys is None else _previous_keys
    if previous is not None and len(keys) != previous["keys"]:
        _invalid(period, "previous_key_count_mismatch")
    for key, entry in _adopted_classifications(
        connection,
        manifest,
        period,
        flow_content,
        allow_absent_financial_reports=allow_absent_financial_reports,
    ):
        tree = _insert(nodes, tree, key, entry, 0, period)
        keys.add(key)
    content = _root_content(period, close_digest, tree, nodes, keys)
    return PreparedClassificationDirectory(
        period,
        close_digest,
        content,
        _sha(content),
        tuple(_reachable_new_nodes(nodes, tree, period).items()) if collect_nodes else (),
    )


def _lookup_many(nodes, tree, keys, period):
    result = {key: () for key in keys}
    pending = {key: tree for key in keys if tree is not None}
    depth = 0
    hashes = {key: _key_hash(key) for key in pending}
    while pending:
        nodes.load_many(pending.values(), period)
        following = {}
        for key, checksum in pending.items():
            node = nodes.read(checksum, period)
            if node["kind"] == "leaf":
                if node["key"] == key:
                    result[key] = tuple(tuple(item) for item in node["entries"])
                continue
            if depth >= 64:
                _invalid(period, "branch_depth_invalid")
            child = node["children"].get(hashes[key][depth])
            if child is not None:
                following[key] = bytes.fromhex(child)
        pending = following
        depth += 1
    return result


def _exceptional_entries(nodes, checksum, period):
    if checksum is None:
        return (), ()
    conflicts, unsafe = [], []
    pending = [checksum]
    while pending:
        nodes.load_many(pending, period)
        following = []
        for item in pending:
            node = nodes.read(item, period)
            if not node["conflicts"] and not node["unsafe"]:
                continue
            if node["kind"] == "leaf":
                if node["conflicts"]:
                    conflicts.append(node["key"])
                unsafe.extend(
                    (node["key"], *entry)
                    for entry in node["entries"]
                    if not entry[3]
                )
            else:
                following.extend(bytes.fromhex(child) for child in node["children"].values())
        pending = following
    return tuple(conflicts), tuple(unsafe)


def classification_directory_scope(connection, end, keys, *, reads=None):
    """Authenticate exact frozen key membership, absence, and exceptional keys.

    No close means there is no frozen history yet. A close without its bound
    root is corruption, never an empty map or a successful absence proof.
    """
    close = connection.execute(
        "SELECT * FROM period_close WHERE period<=? ORDER BY period DESC LIMIT 1", (end,)
    ).fetchone()
    if close is None:
        return None
    cache = (
        reads._report_snapshot_cache
        if reads is not None and reads._snapshot_active and reads.connection is connection
        else None
    )
    cache_key = ("report_classification_directory", close["period"])
    if cache is not None and cache_key in cache:
        root, nodes, key_filter = cache[cache_key]
    else:
        nodes = _Nodes(connection)
        root = _read_root(connection, close, nodes=nodes)
        key_filter = decode_keys_filter(root["key_filter"])
        if cache is not None:
            cache[cache_key] = root, nodes, key_filter
    tree = bytes.fromhex(root["tree"]) if root["tree"] else None
    requested = sorted(set(keys))
    possible = [key for key in requested if may_contain(key_filter, key)]
    membership = {key: () for key in requested}
    membership.update(_lookup_many(nodes, tree, possible, close["period"]))
    conflicts, unsafe = _exceptional_entries(nodes, tree, close["period"])
    if len(conflicts) != root["conflicts"] or len(unsafe) != root["unsafe"]:
        _invalid(close["period"], "exception_count_mismatch")
    conflict_membership = _lookup_many(nodes, tree, conflicts, close["period"])
    if any(len(entries) < 2 for entries in conflict_membership.values()):
        _invalid(close["period"], "conflict_membership_mismatch")
    return {
        "period": close["period"],
        "membership": membership,
        "conflicts": conflicts,
        "conflict_membership": conflict_membership,
        "unsafe": unsafe,
        "root": root,
    }


def compare_classification_directory(
    engine, connection, *, through_period=None, _verified_closes=None, _expected_flows=None
):
    """Rebuild all roots and nodes from preserved closes and actual fact headers."""
    from .content_history_context import close_reader
    from .report_flow_v1 import compare_report_flow

    closes = list(connection.execute(
        "SELECT * FROM period_close "
        + ("WHERE period<=? " if through_period is not None else "")
        + "ORDER BY period",
        (through_period,) if through_period is not None else (),
    ))
    from .verified_close_archive import VerifiedCloseArchive

    if _verified_closes is None:
        decoded = None
    elif isinstance(_verified_closes, VerifiedCloseArchive):
        decoded = _verified_closes.lookup(connection, closes)
    else:
        decoded = {row["period"]: manifest for row, manifest in _verified_closes}
        if len(decoded) != len(closes):
            _invalid(-1, "verified_close_scope_mismatch")
    flows = _expected_flows
    if flows is None:
        flows = compare_report_flow(
            engine, connection, through_period=through_period
        )["expected_rows"]
    flow_by_period = {row[0]: row[1] for row in flows}
    if len(flow_by_period) != len(closes):
        _invalid(-1, "flow_scope_mismatch")
    nodes = _Nodes(connection, overlay={})
    previous = None
    previous_keys = set()
    expected_roots = []
    expected_nodes = {}
    for close in closes:
        period = close["period"]
        if decoded is None:
            manifest = close_reader().decode_close(connection, close)
        elif isinstance(_verified_closes, VerifiedCloseArchive):
            manifest = decoded[period][1]
        else:
            manifest = decoded.get(period)
        flow = flow_by_period.get(period)
        if manifest is None or flow is None:
            _invalid(period, "source_scope_mismatch")
        prepared = prepare_classification_directory(
            connection,
            period,
            close["digest"],
            manifest,
            flow,
            previous=previous,
            nodes=nodes,
            collect_nodes=False,
            allow_absent_financial_reports="financial_reports" not in manifest.get("readiness", {}),
            _previous_keys=previous_keys,
        )
        header = close_reader().verified_header(connection, close, require_marker=True)
        if close_reader().derived_root(header, DERIVED_ROOT_NAME) != prepared.root_digest:
            _invalid(period, "authoritative_root_mismatch")
        expected_roots.append(
            (period, close["digest"], prepared.content, prepared.root_digest)
        )
        tree = json.loads(prepared.content)["tree"]
        expected_nodes.update(
            _reachable_new_nodes(nodes, bytes.fromhex(tree) if tree else None, period)
        )
        previous = json.loads(prepared.content)
        del manifest
    actual_roots = [
        tuple(row)
        for row in connection.execute(
            "SELECT period,close_digest,content,root_digest "
            "FROM report_classification_directory "
            + ("WHERE period<=? " if through_period is not None else "")
            + "ORDER BY period",
            (through_period,) if through_period is not None else (),
        )
    ]
    actual_nodes = {}
    if through_period is None:
        actual_nodes = {
            row["digest"]: row["content"]
            for row in connection.execute("SELECT digest,content FROM report_classification_node")
        }
    else:
        for checksum in expected_nodes:
            row = connection.execute(
                "SELECT content FROM report_classification_node WHERE digest=?", (checksum,)
            ).fetchone()
            if row is not None:
                actual_nodes[checksum] = row["content"]
    return {
        "changed": actual_roots != expected_roots or actual_nodes != expected_nodes,
        "roots": len(expected_roots),
        "nodes": len(expected_nodes),
        "expected_roots": expected_roots,
        "expected_nodes": expected_nodes,
    }


def require_classification_directory(engine, connection, **options):
    result = compare_classification_directory(engine, connection, **options)
    if result["changed"]:
        _invalid(-1, "projection_mismatch")
    return {"roots": result["roots"], "nodes": result["nodes"], "changed": False}


