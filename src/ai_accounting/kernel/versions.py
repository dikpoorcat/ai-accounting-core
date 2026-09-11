"""Known schema contracts and transactional forward upgrades.

Only packaged, fingerprinted sqlite_schema snapshots are accepted old formats.
Directory and company versions advance independently. DDL is validated
independently of the version advertised by database rows.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from .contracts import KernelError
from .types import canonical

HISTORY_DDL = """
CREATE TABLE schema_history(version INTEGER PRIMARY KEY, fingerprint BLOB NOT NULL
 CHECK(length(fingerprint)=32), installed_at TEXT NOT NULL
 DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))) STRICT;
CREATE TRIGGER immutable_schema_history_update BEFORE UPDATE ON schema_history
 BEGIN SELECT RAISE(ABORT,'immutable schema history'); END;
CREATE TRIGGER immutable_schema_history_delete BEFORE DELETE ON schema_history
 BEGIN SELECT RAISE(ABORT,'immutable schema history'); END;
"""


def execute_statements(connection, script: str):
    """Execute DDL without executescript's implicit COMMIT of the caller's transaction."""
    pending = ""
    for char in script:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            connection.execute(pending)
            pending = ""
    if pending.strip():
        raise ValueError("incomplete migration statement")


def objects(connection):
    return [
        dict(zip(("type", "name", "sql"), row, strict=True))
        for row in connection.execute(
            "SELECT type,name,sql FROM sqlite_schema WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ]


def fingerprint(items):
    return hashlib.sha256(canonical(items).encode()).digest()


@lru_cache(maxsize=8)
def baseline(kind):
    data = json.loads(
        (Path(__file__).with_name("migrations") / f"v1_{kind}.json").read_text("utf-8")
    )
    if fingerprint(data["objects"]).hex() != data["sha256"]:
        raise RuntimeError("packaged database contract is damaged")
    return data


def current_version(kind):
    if kind == "business":
        from .schema import VERSION
    elif kind == "catalog":
        from .catalog import VERSION
    else:
        raise ValueError("unknown database kind")
    return VERSION


@lru_cache(maxsize=8)
def known_contracts(kind):
    if kind not in {"business", "catalog"}:
        raise ValueError("unknown database kind")
    contracts = {}
    for path in Path(__file__).with_name("migrations").glob(f"v*_{kind}.json"):
        data = json.loads(path.read_text("utf-8"))
        if fingerprint(data["objects"]).hex() != data["sha256"]:
            raise RuntimeError("packaged database contract is damaged")
        if data["version"] in contracts:
            raise RuntimeError("duplicate database version contract")
        contracts[data["version"]] = data
    return contracts


def check_released_contract(script, *, kind, registry=None):
    """Released production DDL cannot silently change while retaining its version."""
    released = known_contracts(kind).get(current_version(kind))
    if released is None:
        return  # A new, as-yet-unreleased forward version is under development.
    if kind == "business":
        from .service import default_registry

        if registry is not None and registry.models != default_registry().models:
            return  # Isolated test/benchmark registries are never public service inputs.
    if contract_fingerprint(script).hex() != released["sha256"]:
        raise KernelError(
            "schema_version_bump_required", "已发布结构发生变化，必须增加前向数据库版本"
        )


@lru_cache(maxsize=32)
def contract(script: str):
    # In-memory SQL compilation is solely a schema oracle, never a company database.
    with closing(sqlite3.connect(":memory:")) as connection:
        execute_statements(connection, script)
        return objects(connection)


@lru_cache(maxsize=32)
def contract_fingerprint(script: str):
    return fingerprint(contract(script))


def record_version(connection, version):
    connection.execute(
        "INSERT INTO schema_history(version,fingerprint) VALUES(?,?)",
        (version, fingerprint(objects(connection))),
    )


def verify_schema(connection, *, kind="business", registry=None, allow_previous=False):
    from .schema import schema_sql

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    current = current_version(kind)
    actual = objects(connection)
    if version == current:
        if kind == "business":
            if registry is None:
                from .service import default_registry

                registry = default_registry()
            script = schema_sql(registry)
        else:
            from .catalog import catalog_sql

            script = catalog_sql()
        check_released_contract(script, kind=kind, registry=registry)
        expected = contract(script)
        expected_hash = contract_fingerprint(script)
    elif allow_previous and version < current and version in known_contracts(kind):
        previous = known_contracts(kind)[version]
        expected = previous["objects"]
        expected_hash = bytes.fromhex(previous["sha256"])
    else:
        raise KernelError(
            "schema_version_unsupported", "数据库版本不受当前程序支持", version=version
        )
    actual_hash = fingerprint(actual)
    if actual_hash != expected_hash:
        actual_map = {(item["type"], item["name"]): item["sql"] for item in actual}
        expected_map = {(item["type"], item["name"]): item["sql"] for item in expected}
        changed = sorted(
            name
            for typ, name in expected_map.keys() | actual_map.keys()
            if expected_map.get((typ, name)) != actual_map.get((typ, name))
        )
        raise KernelError(
            "schema_fingerprint_mismatch", "数据库结构与已发布版本不一致，拒绝写入", objects=changed
        )
    if kind == "business":
        row = connection.execute("SELECT schema_version FROM identity WHERE id=1").fetchone()
        if row is None or row[0] != version:
            raise KernelError("schema_version_mismatch", "数据库身份与结构版本不一致")
    elif any(item["name"] == "catalog_identity" for item in expected):
        row = connection.execute(
            "SELECT schema_version FROM catalog_identity WHERE id=1"
        ).fetchone()
        if row is None or row[0] != version:
            raise KernelError("schema_version_mismatch", "目录身份与结构版本不一致")
    if any(item["name"] == "schema_history" for item in expected):
        row = connection.execute(
            "SELECT fingerprint FROM schema_history WHERE version=?", (version,)
        ).fetchone()
        if row is None or row[0] != actual_hash:
            raise KernelError("schema_history_mismatch", "数据库结构版本登记不一致")
    return version


def upgrade(connection, *, kind="business", registry=None, fault=None):
    """Upgrade a known contract with additive tables and replaced triggers, atomically."""
    from .schema import schema_sql

    target_version = current_version(kind)

    connection.execute("BEGIN IMMEDIATE")
    try:
        old_version = verify_schema(connection, kind=kind, registry=registry, allow_previous=True)
        if old_version == target_version:
            connection.rollback()
            return False
        if kind == "business":
            if registry is None:
                from .service import default_registry

                registry = default_registry()
            script = schema_sql(registry)
        else:
            from .catalog import catalog_sql

            script = catalog_sql()
        check_released_contract(script, kind=kind, registry=registry)
        old = {(row["type"], row["name"]): row for row in objects(connection)}
        old_hash = fingerprint(list(old.values()))
        new = {(row["type"], row["name"]): row for row in contract(script)}
        for key, item in old.items():
            if key not in new or new[key]["sql"] != item["sql"]:
                if item["type"] != "trigger":
                    raise KernelError(
                        "migration_not_declared", "结构变化缺少明确的前向迁移", object=item["name"]
                    )
                connection.execute(f'DROP TRIGGER "{item["name"]}"')
        # sqlite_schema sort order is not dependency order; create tables before indexes/triggers.
        for typ in ("table", "index", "view", "trigger"):
            for key, item in new.items():
                if item["type"] == typ and (key not in old or old[key]["sql"] != item["sql"]):
                    connection.execute(item["sql"])
        if fault:
            fault("after_ddl")
        if kind == "business":
            connection.execute("DROP TRIGGER immutable_identity_UPDATE")
            connection.execute("UPDATE identity SET schema_version=? WHERE id=1", (target_version,))
            connection.execute(new[("trigger", "immutable_identity_UPDATE")]["sql"])
        else:
            import uuid

            if ("table", "catalog_identity") in old:
                connection.execute("DROP TRIGGER immutable_catalog_identity_update")
                connection.execute(
                    "UPDATE catalog_identity SET schema_version=? WHERE id=1", (target_version,)
                )
                connection.execute(new[("trigger", "immutable_catalog_identity_update")]["sql"])
            else:
                connection.execute(
                    "INSERT INTO catalog_identity VALUES(1,?,?)", (uuid.uuid4().hex, target_version)
                )
        connection.execute(f"PRAGMA user_version={target_version}")
        if not connection.execute(
            "SELECT 1 FROM schema_history WHERE version=?", (old_version,)
        ).fetchone():
            connection.execute(
                "INSERT INTO schema_history(version,fingerprint) VALUES(?,?)",
                (old_version, old_hash),
            )
        record_version(connection, target_version)
        verify_schema(connection, kind=kind, registry=registry)
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise KernelError("migration_integrity_failed", "升级后引用校验失败")
        if fault:
            fault("before_commit")
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise
