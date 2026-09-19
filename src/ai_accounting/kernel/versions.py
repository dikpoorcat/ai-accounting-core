"""Exact family-bound schema recognition and transactional declared upgrades."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from functools import lru_cache

from .contracts import KernelError
from .types import canonical

META_DDL = """
CREATE TABLE schema_meta(id INTEGER PRIMARY KEY CHECK(id=1), family TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN('company','catalog')),
 status TEXT NOT NULL CHECK(status IN('draft','released'))) STRICT;
CREATE TRIGGER immutable_schema_meta_update BEFORE UPDATE ON schema_meta
 BEGIN SELECT RAISE(ABORT,'immutable schema metadata'); END;
CREATE TRIGGER immutable_schema_meta_delete BEFORE DELETE ON schema_meta
 BEGIN SELECT RAISE(ABORT,'immutable schema metadata'); END;
"""
HISTORY_DDL = """
CREATE TABLE schema_history(version INTEGER PRIMARY KEY CHECK(version>=0),
 fingerprint BLOB NOT NULL CHECK(length(fingerprint)=32), installed_at TEXT NOT NULL
 DEFAULT(strftime('%Y-%m-%dT%H:%M:%fZ','now'))) STRICT;
CREATE TRIGGER immutable_schema_history_update BEFORE UPDATE ON schema_history
 BEGIN SELECT RAISE(ABORT,'immutable schema history'); END;
CREATE TRIGGER immutable_schema_history_delete BEFORE DELETE ON schema_history
 BEGIN SELECT RAISE(ABORT,'immutable schema history'); END;
"""


def execute_statements(connection, script):
    """Execute without executescript's implicit commit of the caller's transaction."""
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


@lru_cache(maxsize=32)
def contract(script):
    with closing(sqlite3.connect(":memory:")) as connection:
        execute_statements(connection, script)
        return objects(connection)


@lru_cache(maxsize=32)
def contract_fingerprint(script):
    return fingerprint(contract(script))


def current_version(kind, *, bundle):
    return bundle.current_versions[kind]


def known_contracts(kind, *, bundle):
    return bundle.contracts[kind]


def check_released_contract(script, *, kind, bundle):
    """Draft and released DDL both require the exact paired package contract."""
    expected = bundle.current(kind)
    if contract(script) != expected["objects"] or (
        contract_fingerprint(script).hex() != expected["sha256"]
    ):
        raise KernelError("schema_fingerprint_mismatch", "生成结构与包内合同不一致")


def record_version(connection, version, *, expected_fingerprint):
    actual = fingerprint(objects(connection))
    if actual.hex() != expected_fingerprint:
        raise KernelError("schema_fingerprint_mismatch", "安装结构与目标合同不一致")
    connection.execute(
        "INSERT INTO schema_history(version,fingerprint) VALUES(?,?)", (version, actual)
    )


def install_metadata(connection, bundle, kind):
    """Record only this actual installation, within the creator's transaction."""
    if not connection.in_transaction:
        raise KernelError("migration_transaction_active", "结构登记需要创建事务")
    expected = bundle.current(kind)
    connection.execute(
        "INSERT INTO schema_meta VALUES(1,?,?,?)", (bundle.family, kind, expected["status"])
    )
    connection.execute(f"PRAGMA application_id={bundle.application_id}")
    connection.execute(f"PRAGMA user_version={expected['version']}")
    record_version(connection, expected["version"], expected_fingerprint=expected["sha256"])
    verify_schema(connection, bundle=bundle, kind=kind)


def _metadata(connection, bundle, kind):
    if connection.execute("PRAGMA application_id").fetchone()[0] != bundle.application_id:
        raise KernelError("schema_family_unsupported", "数据库不属于当前系统格式")
    try:
        rows = connection.execute("SELECT id,family,kind,status FROM schema_meta").fetchall()
    except sqlite3.Error as exc:
        raise KernelError("schema_family_unsupported", "数据库缺少当前格式身份") from exc
    if len(rows) != 1 or tuple(rows[0])[:3] != (1, bundle.family, kind):
        raise KernelError("schema_family_unsupported", "数据库系统或类别不匹配")
    status = rows[0][3]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if not (status == "draft" and version == 0 or status == "released" and version >= 1):
        raise KernelError("schema_version_unsupported", "数据库状态与版本不匹配", version=version)
    return status, version


def verify_schema(connection, *, bundle, kind="company", allow_previous=False):
    owned_snapshot = not connection.in_transaction
    if owned_snapshot:
        connection.execute("BEGIN")
    try:
        return _verify_schema(connection, bundle=bundle, kind=kind, allow_previous=allow_previous)
    finally:
        if owned_snapshot:
            connection.rollback()


def _verify_schema(connection, *, bundle, kind, allow_previous):
    if kind not in bundle.contracts:
        raise ValueError("unknown database kind")
    status, version = _metadata(connection, bundle, kind)
    current = bundle.current(kind)
    expected = bundle.contracts[kind].get(version)
    accepted = status == current["status"] and version == current["version"]
    if allow_previous and status == current["status"] == "released":
        accepted = 1 <= version <= current["version"]
    if not accepted or expected is None or expected["status"] != status:
        raise KernelError(
            "schema_version_unsupported", "数据库版本不受当前程序支持", version=version
        )
    actual = objects(connection)
    actual_hash = fingerprint(actual)
    if actual != expected["objects"] or actual_hash.hex() != expected["sha256"]:
        before = {(row["type"], row["name"]): row["sql"] for row in expected["objects"]}
        after = {(row["type"], row["name"]): row["sql"] for row in actual}
        changed = sorted(
            key[1] for key in before.keys() | after.keys() if before.get(key) != after.get(key)
        )
        raise KernelError(
            "schema_fingerprint_mismatch", "数据库结构与包内合同不一致", objects=changed
        )
    history = connection.execute(
        "SELECT version,fingerprint FROM schema_history ORDER BY version"
    ).fetchall()
    if not history or history[-1][0] != version or (status == "draft" and len(history) != 1):
        raise KernelError("schema_history_mismatch", "数据库安装历史与当前版本不一致")
    for installed, recorded in history:
        released = bundle.contracts[kind].get(installed)
        if (
            released is None
            or released["status"] != status
            or (bytes(recorded).hex() != released["sha256"])
        ):
            raise KernelError("schema_history_mismatch", "数据库安装历史指纹不匹配")
    # Missing intermediate versions are valid only for an explicitly declared jump.
    for previous, target in zip(history, history[1:], strict=False):
        if not any(
            step.family == bundle.family
            and step.kind == kind
            and step.source_version == previous[0]
            and step.target_version == target[0]
            and step.source_sha256 == bytes(previous[1]).hex()
            and step.target_sha256 == bytes(target[1]).hex()
            for step in bundle.steps
        ):
            raise KernelError("schema_history_mismatch", "安装历史缺少对应的明确迁移")
    table, fields = (
        ("identity", ("company_id", "taxpayer_id", "database_id"))
        if kind == "company"
        else ("catalog_identity", ("instance_id",))
    )
    try:
        identities = connection.execute(f"SELECT id,{','.join(fields)} FROM {table}").fetchall()
    except sqlite3.Error as exc:
        raise KernelError("schema_identity_mismatch", "数据库身份记录无效") from exc
    if (
        len(identities) != 1
        or identities[0][0] != 1
        or any(not isinstance(value, str) or not value.strip() for value in identities[0][1:])
    ):
        raise KernelError("schema_identity_mismatch", "数据库需要唯一且完整的身份记录")
    return version


def database_format(connection, *, bundle, kind="company", allow_previous=False):
    version = verify_schema(connection, bundle=bundle, kind=kind, allow_previous=allow_previous)
    expected = bundle.contracts[kind][version]
    return {key: expected[key] for key in ("family", "kind", "status", "version")} | {
        "fingerprint": expected["sha256"]
    }


def _assert_step_contract(connection, version, sha256, *, bundle, kind):
    status, actual_version = _metadata(connection, bundle, kind)
    if (
        status != "released"
        or actual_version != version
        or (fingerprint(objects(connection)).hex() != sha256)
    ):
        raise KernelError("schema_fingerprint_mismatch", "迁移步骤的精确结构合同不匹配")


def _execute_steps(connection, steps, *, bundle, kind, verify_source, finish_step, fault=None):
    """The same atomic executor serves packaged and isolated synthetic contracts."""
    if connection.in_transaction:
        raise KernelError("migration_transaction_active", "升级必须从无事务连接开始")
    if not steps:
        return False
    for index, step in enumerate(steps):
        if (
            step.family != bundle.family
            or step.kind != kind
            or (
                index
                and (
                    steps[index - 1].target_version != step.source_version
                    or steps[index - 1].target_sha256 != step.source_sha256
                )
            )
        ):
            raise KernelError("migration_not_declared", "迁移步骤未形成同系统连续合同")
    verify_source(connection)
    try:
        if any(step.requires_fk_off for step in steps):
            connection.execute("PRAGMA foreign_keys=OFF")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise KernelError("migration_integrity_failed", "无法关闭迁移外键检查")
        connection.execute("BEGIN IMMEDIATE")
        verify_source(connection)
        for step in steps:
            _assert_step_contract(
                connection, step.source_version, step.source_sha256, bundle=bundle, kind=kind
            )
            step.apply(connection)
            if fault:
                fault("after_ddl")
            finish_step(connection, step)
            _assert_step_contract(
                connection, step.target_version, step.target_sha256, bundle=bundle, kind=kind
            )
            step.validate_data(connection)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise KernelError("migration_integrity_failed", "升级后引用校验失败")
            if fault:
                fault("after_validate")
        if fault:
            fault("before_commit")
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise
    finally:
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise KernelError("migration_integrity_failed", "升级后无法恢复外键检查")
        except BaseException:
            connection.close()
            raise


def _migration_plan(bundle, kind, source, target):
    if source["status"] != "released" or target["status"] != "released":
        raise KernelError("schema_version_unsupported", "开发结构不提供升级路径")
    steps, current = [], source
    while current["version"] < target["version"]:
        candidates = [
            step
            for step in bundle.steps
            if step.family == bundle.family
            and step.kind == kind
            and step.source_version == current["version"]
            and step.source_sha256 == current["sha256"]
            and step.target_version <= target["version"]
        ]
        if len(candidates) != 1:
            raise KernelError("migration_not_declared", "结构变化缺少唯一明确的前向迁移")
        step = candidates[0]
        next_contract = bundle.contracts[kind].get(step.target_version)
        if next_contract is None or next_contract["sha256"] != step.target_sha256:
            raise KernelError("migration_not_declared", "迁移目标与包内合同不匹配")
        steps.append(step)
        current = next_contract
    if current["version"] != target["version"] or current["sha256"] != target["sha256"]:
        raise KernelError("migration_not_declared", "迁移不能到达目标结构合同")
    return tuple(steps)


def upgrade(connection, *, bundle, kind="company", fault=None):
    if connection.in_transaction:
        raise KernelError("migration_transaction_active", "升级必须从无事务连接开始")
    version = verify_schema(connection, bundle=bundle, kind=kind, allow_previous=True)
    target = bundle.current(kind)
    if version == target["version"]:
        return False
    steps = _migration_plan(bundle, kind, bundle.contracts[kind][version], target)

    def finish(connection, step):
        if fingerprint(objects(connection)).hex() != step.target_sha256:
            raise KernelError("schema_fingerprint_mismatch", "迁移结构不符合目标合同")
        connection.execute(f"PRAGMA user_version={step.target_version}")
        record_version(connection, step.target_version, expected_fingerprint=step.target_sha256)
        verify_schema(connection, bundle=bundle, kind=kind, allow_previous=True)

    return _execute_steps(
        connection,
        steps,
        bundle=bundle,
        kind=kind,
        verify_source=lambda conn: verify_schema(
            conn, bundle=bundle, kind=kind, allow_previous=True
        ),
        finish_step=finish,
        fault=fault,
    )
