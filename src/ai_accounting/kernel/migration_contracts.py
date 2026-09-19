"""Read-only expansion of packaged SQLite contracts; SQL text is never normalized."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .types import canonical

# These released full snapshots remain readable; every later contract is a delta.
_FULL_CONTRACT_VERSIONS = {
    "business": frozenset(range(1, 13)),
    "catalog": frozenset({0, 2, 3}),
}


def _digest(items):
    return hashlib.sha256(canonical(items).encode()).hexdigest()


def _object_map(items, *, sql):
    result = {}
    if not isinstance(items, list):
        raise RuntimeError("invalid database contract objects")
    fields = {"type", "name", "sql"} if sql else {"type", "name"}
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != fields
            or any(not isinstance(value, str) or not value for value in item.values())
            or item["type"] not in {"table", "index", "trigger", "view"}
        ):
            raise RuntimeError("invalid database contract object")
        key = item["type"], item["name"]
        if key in result:
            raise RuntimeError("duplicate database contract object")
        result[key] = item
    return result


def load_contracts(directory: Path, kind: str):
    """Expand a same-kind, strictly descending parent chain, validating every digest."""
    if kind not in {"business", "catalog"}:
        raise ValueError("unknown database kind")
    raw = {}
    for path in directory.glob(f"v*_{kind}.json"):
        try:
            data = json.loads(path.read_text("utf-8"))
            version = data["version"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("invalid database contract") from exc
        # The frozen initial catalog file is named v1 but advertises user_version 0.
        initial_catalog = kind == "catalog" and version == 0 and path.name == "v1_catalog.json"
        if (
            type(version) is not int
            or version < 0
            or (not initial_catalog and (version == 0 or path.name != f"v{version}_{kind}.json"))
        ):
            raise RuntimeError("invalid database version contract")
        if version in raw:
            raise RuntimeError("duplicate database version contract")
        raw[version] = data
    expanded = {}

    def expand(version):
        if version in expanded:
            return expanded[version]
        if version not in raw:
            raise RuntimeError("missing database parent contract")
        data = raw[version]
        if "objects" in data:
            if version not in _FULL_CONTRACT_VERSIONS[kind]:
                raise RuntimeError("new database contracts must be incremental")
            if (
                not {"version", "objects", "sha256"} <= data.keys()
                or (data.keys() - {"version", "objects", "sha256", "kind", "source_commit"})
                or ("kind" in data and data["kind"] != kind)
            ):
                raise RuntimeError("invalid full database contract")
            items = _object_map(data["objects"], sql=True)
        else:
            if set(data) != {
                "version",
                "base_version",
                "base_sha256",
                "add",
                "remove",
                "replace",
                "sha256",
            }:
                raise RuntimeError("invalid incremental database contract")
            base = data["base_version"]
            if type(base) is not int or not 0 <= base < version:
                raise RuntimeError("invalid database parent version")
            parent = expand(base)
            if parent["sha256"] != data["base_sha256"]:
                raise RuntimeError("database parent fingerprint mismatch")
            items = _object_map(parent["objects"], sql=True)
            add = _object_map(data["add"], sql=True)
            remove = _object_map(data["remove"], sql=False)
            replace = _object_map(data["replace"], sql=True)
            if (
                add.keys() & remove.keys()
                or add.keys() & replace.keys()
                or (remove.keys() & replace.keys())
            ):
                raise RuntimeError("overlapping database contract operations")
            if add.keys() & items.keys() or (remove.keys() | replace.keys()) - items.keys():
                raise RuntimeError("invalid database contract operation target")
            for key in remove:
                del items[key]
            items.update(replace)
            items.update(add)
        result = [items[key] for key in sorted(items)]
        if _digest(result) != data["sha256"]:
            raise RuntimeError("packaged database contract is damaged")
        expanded[version] = {"version": version, "objects": result, "sha256": data["sha256"]}
        return expanded[version]

    for version in sorted(raw):
        expand(version)
    return expanded


def diff_contracts(old, new):
    """Return exact changed objects and both SQL strings, suitable for review."""
    before = _object_map(old["objects"], sql=True)
    after = _object_map(new["objects"], sql=True)
    return [
        {
            "type": key[0],
            "name": key[1],
            "old_sql": before.get(key, {}).get("sql"),
            "new_sql": after.get(key, {}).get("sql"),
        }
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]
