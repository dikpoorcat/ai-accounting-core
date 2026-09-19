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
from .migration_contracts import load_contracts
from .migration_steps import MigrationStep
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


def baseline(kind):
    return known_contracts(kind)[0 if kind == "catalog" else 1]


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
    return load_contracts(Path(__file__).with_name("migrations"), kind)


@lru_cache(maxsize=1)
def recorded_business_v10_variant():
    """Exact T4 intermediate DDL, accepted only by the forward-upgrade probe.

    This is not another released v10 contract. Its complete object snapshot and
    pinned digest preserve the recorded source without changing v1-v10 history.
    """
    expected_hash = "45dd0bba8f9668b552859ff7beea612a8c51fd9838f1a3d9785c76771bc08266"
    data = json.loads(
        (
            Path(__file__).with_name("migrations")
            / "recorded_business_v10_pre_account_indexes.json"
        ).read_text("utf-8")
    )
    if (
        data["version"] != 10
        or data["sha256"] != expected_hash
        or fingerprint(data["objects"]).hex() != expected_hash
    ):
        raise RuntimeError("packaged historical database contract is damaged")
    return data


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
    if kind == "business" and allow_previous and version == 10 and version < current:
        if actual_hash != expected_hash:
            recorded = recorded_business_v10_variant()
            if actual_hash.hex() == recorded["sha256"] and actual == recorded["objects"]:
                expected = recorded["objects"]
                expected_hash = bytes.fromhex(recorded["sha256"])
        # Identity and the original v10 schema_history row must still match
        # below. Near matches and current-version files never use this branch.
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


def _legacy_apply(connection, *, kind, source, target, fault):
    """Frozen compatibility bridge, not a recipe for future structure versions."""
    old_version, target_version = source["version"], target["version"]
    old = {(row["type"], row["name"]): row for row in source["objects"]}
    new = {(row["type"], row["name"]): row for row in target["objects"]}
    for key, item in old.items():
        if key not in new or new[key]["sql"] != item["sql"]:
            if item["type"] != "trigger":
                raise KernelError(
                    "migration_not_declared", "结构变化缺少明确的前向迁移", object=item["name"]
                )
            connection.execute(f'DROP TRIGGER "{item["name"]}"')
    # sqlite_schema sort order is not dependency order; create tables before indexes/triggers.
    for typ in ("table", "index", "view", "trigger"):
        if typ == "trigger" and kind == "business" and old_version < 9 <= target_version:
            # V8 retained only an irreversible context digest. Mark those exact
            # pre-upgrade records before runtime insert guards are installed;
            # never attach today's content to a historical commentary.
            connection.execute(
                "INSERT INTO period_commentary_basis(commentary_id,contract) "
                "SELECT id,'legacy-context-v8' FROM period_commentary_revision"
            )
        for key, item in new.items():
            if item["type"] == typ and (key not in old or old[key]["sql"] != item["sql"]):
                connection.execute(item["sql"])
    if kind == "business" and old_version < 10 <= target_version:
        from .read_indexes import backfill_read_indexes

        backfill_read_indexes(connection)
        if fault:
            fault("after_read_indexes")


def _legacy_finish(connection, *, kind, source, target):
    old_version, target_version = source["version"], target["version"]
    old = {(row["type"], row["name"]): row for row in source["objects"]}
    new = {(row["type"], row["name"]): row for row in target["objects"]}
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
            (old_version, bytes.fromhex(source["sha256"])),
        )
    record_version(connection, target_version)


def _assert_step_contract(connection, version, sha256):
    if connection.execute("PRAGMA user_version").fetchone()[0] != version or (
        fingerprint(objects(connection)).hex() != sha256
    ):
        raise KernelError("schema_fingerprint_mismatch", "迁移步骤的精确结构合同不匹配")


def _execute_steps(connection, steps, *, kind, verify_source, finish_step, fault=None):
    """One transaction for package-owned steps; recheck the source under a write lock.

    This is also the execution path for isolated test contracts. The caller owns
    identity/history writes because those tables are part of each target contract.
    """
    if connection.in_transaction:
        raise KernelError("migration_transaction_active", "升级必须从无事务连接开始")
    if not steps:
        return False
    for index, step in enumerate(steps):
        if step.kind != kind or (
            index
            and (
                steps[index - 1].target_version != step.source_version
                or steps[index - 1].target_sha256 != step.source_sha256
            )
        ):
            raise KernelError("migration_not_declared", "迁移步骤未形成同类连续合同")
    verify_source(connection)
    _assert_step_contract(connection, steps[0].source_version, steps[0].source_sha256)
    try:
        if any(step.requires_fk_off for step in steps):
            connection.execute("PRAGMA foreign_keys=OFF")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise KernelError("migration_integrity_failed", "无法关闭迁移外键检查")
        connection.execute("BEGIN IMMEDIATE")
        verify_source(connection)
        for step in steps:
            _assert_step_contract(connection, step.source_version, step.source_sha256)
            step.apply(connection)
            if fault:
                fault("after_ddl")
            finish_step(connection, step)
            _assert_step_contract(connection, step.target_version, step.target_sha256)
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


# Future releases add explicit package-owned steps here; this stage adds no version.
_DECLARED_STEPS: tuple[MigrationStep, ...] = ()


def _legacy_step(kind, source, target, fault):
    def validate_data(connection):
        if kind == "business" and source["version"] < 9:
            missing = connection.execute(
                "SELECT 1 FROM period_commentary_revision r "
                "LEFT JOIN period_commentary_basis b ON b.commentary_id=r.id "
                "WHERE b.commentary_id IS NULL LIMIT 1"
            ).fetchone()
            if missing:
                raise KernelError("migration_integrity_failed", "历史经营说明迁移不完整")

    return MigrationStep(
        kind,
        source["version"],
        source["sha256"],
        target["version"],
        target["sha256"],
        lambda connection: _legacy_apply(
            connection, kind=kind, source=source, target=target, fault=fault
        ),
        validate_data,
    )


def _migration_plan(kind, source, target, *, fault=None):
    """Resolve exact packaged steps, retaining the frozen legacy direct bridge."""
    endpoint = {"business": 12, "catalog": 3}[kind]
    steps = []
    current = source
    if current["version"] < endpoint <= target["version"]:
        bridge_target = target if target["version"] == endpoint else known_contracts(kind)[endpoint]
        steps.append(_legacy_step(kind, current, bridge_target, fault))
        current = bridge_target
    while current["version"] < target["version"]:
        candidates = [
            step
            for step in _DECLARED_STEPS
            if (
                step.kind == kind
                and step.source_version == current["version"]
                and step.source_sha256 == current["sha256"]
                and step.target_version <= target["version"]
            )
        ]
        if len(candidates) != 1:
            raise KernelError("migration_not_declared", "结构变化缺少唯一明确的前向迁移")
        step = candidates[0]
        next_contract = known_contracts(kind).get(step.target_version)
        if next_contract is None or next_contract["sha256"] != step.target_sha256:
            raise KernelError("migration_not_declared", "迁移目标与包内结构合同不匹配")
        steps.append(step)
        current = next_contract
    if current["version"] != target["version"] or current["sha256"] != target["sha256"]:
        raise KernelError("migration_not_declared", "迁移不能到达目标结构合同")
    return tuple(steps)


def upgrade(connection, *, kind="business", registry=None, fault=None):
    """Upgrade known package contracts, preserving skipped-version history semantics."""
    if connection.in_transaction:
        raise KernelError("migration_transaction_active", "升级必须从无事务连接开始")
    old_version = verify_schema(connection, kind=kind, registry=registry, allow_previous=True)
    target_version = current_version(kind)
    if old_version == target_version:
        return False
    if kind == "business":
        from .schema import schema_sql
        from .service import default_registry

        script = schema_sql(registry if registry is not None else default_registry())
    else:
        from .catalog import catalog_sql

        script = catalog_sql()
    check_released_contract(script, kind=kind, registry=registry)
    target = {
        "version": target_version,
        "objects": contract(script),
        "sha256": contract_fingerprint(script).hex(),
    }
    source = known_contracts(kind)[old_version]
    if source["sha256"] != fingerprint(objects(connection)).hex():
        if kind != "business" or old_version != 10:
            raise KernelError("schema_fingerprint_mismatch", "迁移源合同不匹配")
        source = recorded_business_v10_variant()

    steps = _migration_plan(kind, source, target, fault=fault)

    def finish(connection, step):
        step_source = {
            "version": step.source_version,
            "sha256": step.source_sha256,
            "objects": source["objects"]
            if step.source_version == old_version
            else known_contracts(kind)[step.source_version]["objects"],
        }
        step_target = (
            target
            if step.target_version == target_version
            else (known_contracts(kind)[step.target_version])
        )
        _legacy_finish(connection, kind=kind, source=step_source, target=step_target)
        verify_schema(connection, kind=kind, registry=registry, allow_previous=True)

    return _execute_steps(
        connection,
        steps,
        kind=kind,
        verify_source=lambda connection: verify_schema(
            connection, kind=kind, registry=registry, allow_previous=True
        ),
        finish_step=finish,
        fault=fault,
    )
