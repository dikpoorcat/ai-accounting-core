"""Exact package-owned SQLite contracts; no legacy formats or SQL normalization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .types import canonical

KINDS = frozenset({"company", "catalog"})
HEADERS = {"family", "kind", "status", "version", "application_id"}


def _digest(items):
    return hashlib.sha256(canonical(items).encode()).hexdigest()


def _object_map(items, *, sql):
    if not isinstance(items, list):
        raise RuntimeError("invalid database contract objects")
    result = {}
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


def load_contracts(directory: Path, kind: str, *, family: str, application_id: int):
    """Read one kind directory; draft is independent of every released parent chain."""
    if kind not in KINDS:
        raise ValueError("unknown database kind")
    raw = {}
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("invalid database contract") from exc
        if not isinstance(data, dict) or not HEADERS <= data.keys():
            raise RuntimeError("invalid database contract header")
        version, status = data["version"], data["status"]
        if (
            data["family"] != family
            or data["kind"] != kind
            or type(data["application_id"]) is not int
            or data["application_id"] != application_id
            or type(version) is not int
            or not (
                status == "draft"
                and version == 0
                and path.name == "draft.json"
                or status == "released"
                and version >= 1
                and path.name == f"v{version}.json"
            )
        ):
            raise RuntimeError("invalid database version contract")
        if version in raw:
            raise RuntimeError("duplicate database version contract")
        raw[version] = data
    if not raw:
        raise RuntimeError("missing database contracts")
    expanded = {}

    def expand(version):
        if version in expanded:
            return expanded[version]
        if version not in raw:
            raise RuntimeError("missing database parent contract")
        data = raw[version]
        if "objects" in data:
            if version not in {0, 1}:
                raise RuntimeError("new database contracts must be incremental")
            if set(data) != HEADERS | {"objects", "sha256"}:
                raise RuntimeError("invalid full database contract")
            items = _object_map(data["objects"], sql=True)
        else:
            if version < 2 or set(data) != HEADERS | {
                "base_version",
                "base_sha256",
                "add",
                "remove",
                "replace",
                "sha256",
            }:
                raise RuntimeError("invalid incremental database contract")
            base = data["base_version"]
            if type(base) is not int or not 1 <= base < version:
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
        expanded[version] = {
            **{key: data[key] for key in HEADERS},
            "objects": result,
            "sha256": data["sha256"],
        }
        return expanded[version]

    for version in sorted(raw):
        expand(version)
    return expanded


def diff_contracts(old, new):
    """Exact changed objects and both SQL strings, suitable for human review."""
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
