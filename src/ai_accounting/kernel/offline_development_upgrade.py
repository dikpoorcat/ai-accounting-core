"""Explicit package-owned draft upgrades that preserve the current accounting root."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from contextlib import closing
from dataclasses import replace
from types import MappingProxyType

from .backup import (
    _retained_history_digest, _verify_connection, copy_to_unpublished_database,
    create_portable, verify_file,
)
from .contracts import KernelError
from .development_contracts import get_contract_sha
from .migration_contracts import diff_contracts
from .offline_upgrade import _catalog_retained_digest, _catalog_rows
from .permissions import create_private_file, ensure_private_directory, reject_reparse_path
from .runtime import connect, private_file_lock, verification_snapshot
from .schema_bundle import production_bundle
from .versions import fingerprint, objects, verify_schema

SOURCE_FINGERPRINT = "923584f720781cb28a369dd5b269537f64a4551034d306e9447bc2935d263861"
INDEX_SOURCE_FINGERPRINT = "c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb"
INDEX_TARGET_FINGERPRINT = "52556e8ec6cbbb81ab9ad9a69bb87897401471dba9c693e7b7ebddd2a5b08bf4"
_DIRECT_ADOPTION_SQL = (
    "CREATE INDEX close_reference_direct_adoption ON close_reference(\n"
    " reference_type,reference_id,path,close_period,position)\n"
    " WHERE path='adopted_results[*].fact_id' OR path='adopted_results[*].calculation_id'"
)
_REPLACEMENTS = {"fact_payment": "counterparty_id", "fact_pass_through": "beneficiary_id"}
_ADDED_TABLES = {"schema_draft_history", "fact_managed_reserve_internal_movement"}
_ADDED_TRIGGERS = {
    "immutable_schema_draft_history_update",
    "immutable_schema_draft_history_delete",
    "immutable_fact_managed_reserve_internal_movement_DELETE",
    "immutable_fact_managed_reserve_internal_movement_UPDATE",
    "owner_fact_managed_reserve_internal_movement",
    "sealed_fact_managed_reserve_internal_movement_insert",
}


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def source_bundle(bundle, source_fingerprint=SOURCE_FINGERPRINT):
    """Decode the preserved exact source with the unchanged existing fact types."""
    source = get_contract_sha(bundle, "company", source_fingerprint)
    if source is None or source_fingerprint not in (SOURCE_FINGERPRINT, INDEX_SOURCE_FINGERPRINT):
        raise KernelError("migration_not_declared", "开发升级来源未明确声明")
    registry = copy.copy(bundle.registry)
    registry.models = dict(registry.models)
    registry.evaluators = dict(registry.evaluators)
    if source_fingerprint == SOURCE_FINGERPRINT:
        registry.models.pop("managed_reserve_internal_movement", None)
        registry.evaluators.pop("managed_reserve_internal_movement", None)
    return replace(
        bundle,
        registry=registry,
        contracts=MappingProxyType(
            {
                **bundle.contracts,
                "company": MappingProxyType({0: source}),
            }
        ),
    )


def _declared_changes(bundle):
    source = get_contract_sha(bundle, "company", SOURCE_FINGERPRINT)
    intermediate = get_contract_sha(bundle, "company", INDEX_SOURCE_FINGERPRINT)
    target = bundle.current("company")
    transitions = ((SOURCE_FINGERPRINT, INDEX_SOURCE_FINGERPRINT),
                   (INDEX_SOURCE_FINGERPRINT, INDEX_TARGET_FINGERPRINT))
    if (
        bundle.status != "draft"
        or source is None
        or intermediate is None
        or target["sha256"] != INDEX_TARGET_FINGERPRINT
        or set(transitions) != bundle.draft_transitions.get("company", frozenset())
    ):
        raise KernelError("migration_not_declared", "开发结构变化缺少明确来源和目标声明")
    differences = diff_contracts(source, intermediate)
    expected = {
        *(("table", name) for name in _REPLACEMENTS),
        *(("table", name) for name in _ADDED_TABLES),
        *(("trigger", name) for name in _ADDED_TRIGGERS),
        ("trigger", "fact_seal_shape"),
    }
    if {(item["type"], item["name"]) for item in differences} != expected:
        raise KernelError("migration_not_declared", "本次开发迁移不包含其他结构变化")
    for item in differences:
        name = item["name"]
        if name in _REPLACEMENTS:
            field = _REPLACEMENTS[name]
            before, after = item["old_sql"], item["new_sql"]
            marker = f'"{field}" TEXT NOT NULL'
            if before.count(marker) != 1 or before.replace(marker, f'"{field}" TEXT') != after:
                raise KernelError("migration_not_declared", "可空交易方迁移与精确声明不一致")
        elif name == "fact_seal_shape":
            fragment = (
                " WHEN 'managed_reserve_internal_movement' THEN EXISTS(SELECT 1 FROM "
                "fact_managed_reserve_internal_movement t WHERE t.revision_id=f.id)"
            )
            if item["new_sql"].count(fragment) != 1 or (
                item["new_sql"].replace(fragment, "") != item["old_sql"]
            ):
                raise KernelError("migration_not_declared", "类型封存迁移超出新类型声明")
        elif item["old_sql"] is not None or item["new_sql"] is None:
            raise KernelError("migration_not_declared", "开发迁移只能添加明确的新对象")
    index_differences = diff_contracts(intermediate, target)
    if index_differences != [{
        "type": "index", "name": "close_reference_direct_adoption",
        "old_sql": None, "new_sql": _DIRECT_ADOPTION_SQL,
    }]:
        raise KernelError("migration_not_declared", "直接采用索引迁移与精确声明不一致")
    return ((source, intermediate, differences), (intermediate, target, index_differences))


def _all_existing_rows(connection, source_contract):
    """Hash every original table value, including projections, requests and job state."""
    digest, counts = hashlib.sha256(), {}
    for name in sorted(
        item["name"] for item in source_contract["objects"] if item["type"] == "table"
    ):
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")]
        if not columns:
            raise KernelError("migration_integrity_failed", "开发升级缺少原始表")
        digest.update(name.encode() + b"\0")
        count = 0
        sql = f"SELECT * FROM {_quote(name)} ORDER BY " + ",".join(map(_quote, columns))
        for row in connection.execute(sql):
            encoded = json.dumps(
                [
                    ["blob", bytes(value).hex()] if isinstance(value, bytes) else ["value", value]
                    for value in row
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
        counts[name] = count
    return digest.hexdigest(), counts


def _replace_table(connection, name, target_objects, *, fault=None):
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")]
    attached = list(
        connection.execute(
            "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name=? "
            "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name",
            (name,),
        )
    )
    for typ, attached_name, sql in attached:
        if target_objects.get((typ, attached_name)) != sql:
            raise KernelError("migration_not_declared", "复制表的关联对象未完整保留")
    staging = "draft_copy_" + name
    connection.execute(f"CREATE TEMP TABLE {_quote(staging)} AS SELECT * FROM {_quote(name)}")
    if fault:
        fault("after_copy")
    connection.execute(f"DROP TABLE {_quote(name)}")
    if fault:
        fault("after_drop")
    # Create under the exact original name. Rename would change SQLite's saved SQL quoting.
    connection.execute(target_objects[("table", name)])
    actual = [row[1] for row in connection.execute(f"PRAGMA table_info({_quote(name)})")]
    if actual != columns:
        raise KernelError("migration_not_declared", "开发复制不得改变原始字段集合和顺序")
    fields = ",".join(map(_quote, columns))
    connection.execute(
        f"INSERT INTO {_quote(name)}({fields}) SELECT {fields} FROM {_quote(staging)}"
    )
    connection.execute(f"DROP TABLE {_quote(staging)}")
    for _, _, sql in attached:
        connection.execute(sql)


def _apply_first_step(connection, target, differences, *, fault=None):
    """The original declared nullable-party/internal-reserve table adjustment."""
    target_objects = {(item["type"], item["name"]): item["sql"] for item in target["objects"]}
    for name in _REPLACEMENTS:
        _replace_table(connection, name, target_objects, fault=fault)
    for item in differences:
        if item["name"] in _ADDED_TABLES:
            connection.execute(item["new_sql"])
    connection.execute("DROP TRIGGER fact_seal_shape")
    connection.execute(target_objects[("trigger", "fact_seal_shape")])
    for item in differences:
        if item["name"] in _ADDED_TRIGGERS:
            connection.execute(item["new_sql"])


def upgrade_company(
    connection, bundle, *, fault=None, expected_source_fingerprint=None,
    expected_company_id=None, expected_database_id=None, expected_taxpayer_id=None,
):
    """Atomic explicit source-to-target DDL; no accounting facts are changed."""
    if connection.in_transaction:
        raise KernelError("migration_transaction_active", "开发升级须从无事务连接开始")
    declared = _declared_changes(bundle)
    identity_checks = {
        "expected_company_id": expected_company_id,
        "expected_database_id": expected_database_id,
        "expected_taxpayer_id": expected_taxpayer_id,
    }
    with verification_snapshot(connection):
        previous = _company_bundle(connection, bundle)
        if (expected_source_fingerprint is not None
                and previous.current("company")["sha256"] != expected_source_fingerprint):
            raise KernelError("schema_fingerprint_mismatch", "开发升级来源在备份后发生变化")
        verify_schema(connection, bundle=previous)
        initial = _verify_connection(connection, previous, allow_previous=False, **identity_checks)
    # Bind standalone calls too: taking the write lock cannot silently adopt
    # another identity with the exact same source structure.
    identity_checks = {"expected_" + key: value for key, value in initial["identity"].items()}
    if previous is bundle:
        return {"status": "verified_skip"}
    source = previous.current("company")
    target = bundle.current("company")
    steps = declared if source["sha256"] == SOURCE_FINGERPRINT else declared[1:]
    replace_tables = source["sha256"] == SOURCE_FINGERPRINT
    try:
        if replace_tables:
            connection.execute("PRAGMA foreign_keys=OFF")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
                raise KernelError("migration_integrity_failed", "无法关闭迁移外键检查")
        if fault:
            fault("before_begin")
        connection.execute("BEGIN IMMEDIATE")
        verify_schema(connection, bundle=previous)
        _verify_connection(connection, previous, allow_previous=False, **identity_checks)
        original_rows = _all_existing_rows(connection, source)
        retained = _retained_history_digest(connection)
        applied = []
        for step_index, (step_source, step_target, differences) in enumerate(steps):
            # Includes existing draft-history rows. Compare before appending the
            # new receipt so every original value remains bound by this hash.
            # The first source is still the exact same locked snapshot as the
            # original receipt baseline; subsequent steps include new receipts.
            step_rows = (original_rows if step_index == 0 else
                         _all_existing_rows(connection, step_source))
            if step_source["sha256"] == SOURCE_FINGERPRINT:
                _apply_first_step(connection, step_target, differences, fault=fault)
            else:
                connection.execute(differences[0]["new_sql"])
            if fault:
                fault("after_ddl" if step_source["sha256"] == SOURCE_FINGERPRINT
                      else "after_index_ddl")
            if (objects(connection) != step_target["objects"]
                    or fingerprint(objects(connection)).hex() != step_target["sha256"]):
                raise KernelError("schema_fingerprint_mismatch", "开发升级未生成精确目标结构")
            if (_all_existing_rows(connection, step_source) != step_rows
                    or _retained_history_digest(connection) != retained):
                raise KernelError("migration_integrity_failed", "开发升级改变了原始数据或保留历史")
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM schema_draft_history"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
                "retained_history_digest) VALUES(?,?,?,?)",
                (sequence, bytes.fromhex(step_source["sha256"]),
                 bytes.fromhex(step_target["sha256"]), bytes.fromhex(retained)),
            )
            step_bundle = (bundle if step_target is target else
                           source_bundle(bundle, step_target["sha256"]))
            verify_schema(connection, bundle=step_bundle)
            applied.append({"sequence": sequence, "source_fingerprint": step_source["sha256"],
                            "target_fingerprint": step_target["sha256"]})
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise KernelError("migration_integrity_failed", "开发升级后引用核验失败")
        result = _verify_connection(connection, bundle, allow_previous=False, **identity_checks)
        if result["verification"]["status"] != "verified" or result["verification"]["limitations"]:
            raise KernelError("migration_integrity_failed", "开发升级目标内容未完整核验")
        if fault:
            fault("before_commit")
        connection.commit()
        return {
            "status": "upgraded",
            "source_fingerprint": source["sha256"],
            "target_fingerprint": target["sha256"],
            "retained_history_digest": retained,
            "original_rows_digest": original_rows[0],
            "original_table_count": len(original_rows[1]),
            "steps": applied,
            "verification": result["verification"],
        }
    except BaseException:
        connection.rollback()
        raise
    finally:
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise KernelError("migration_integrity_failed", "开发升级后无法恢复外键检查")
        except BaseException:
            connection.close()
            raise


def _company_bundle(connection, bundle):
    actual = fingerprint(objects(connection)).hex()
    if actual == bundle.current("company")["sha256"]:
        verify_schema(connection, bundle=bundle)
        return bundle
    if actual in (SOURCE_FINGERPRINT, INDEX_SOURCE_FINGERPRINT):
        previous = source_bundle(bundle, actual)
        verify_schema(connection, bundle=previous)
        return previous
    raise KernelError("schema_fingerprint_mismatch", "当前库不属于本次开发升级的精确来源或目标")


def upgrade_root(root, *, bundle=None, fault=None):
    """Stop is explicit; verified backups precede any registered company upgrade."""
    bundle = bundle or production_bundle()
    _declared_changes(bundle)
    root = reject_reparse_path(root)
    if not (root / "catalog.sqlite").is_file():
        raise KernelError("database_root_unrecognized", "开发升级只接续已有目录")
    with private_file_lock(root / ".resident.lock") as acquired:
        if not acquired:
            raise KernelError("service_active", "请先 stop 当前服务，再执行显式开发升级")
        with closing(connect(root / "catalog.sqlite", read_only=True)) as catalog:
            _, rows = _catalog_rows(
                catalog, root, bundle, allow_previous=False, require_completed=True
            )
            catalog_digest = _catalog_retained_digest(catalog)
        sources = {}
        for row in rows:
            with closing(connect(row["path"], read_only=True)) as connection:
                sources[row["id"]] = _company_bundle(connection, bundle)
                if connection.execute(
                    "SELECT 1 FROM jobs WHERE status IN('pending','running') LIMIT 1"
                ).fetchone():
                    raise KernelError("company_operation_incomplete", "请先处理未完成后台任务")
            verify_file(
                row["path"],
                _bundle=sources[row["id"]],
                **{
                    "expected_company_id": row["id"],
                    "expected_database_id": row["database_id"],
                    "expected_taxpayer_id": row["taxpayer_id"],
                },
            )
        operation = uuid.uuid4().hex
        backup = ensure_private_directory(root / ".development-upgrades" / operation, parents=True)
        catalog_copy = create_private_file(backup / "catalog.sqlite")
        with closing(connect(root / "catalog.sqlite", read_only=True)) as current:
            with closing(connect(catalog_copy)) as saved:
                copy_to_unpublished_database(current, saved)
                verify_schema(saved, bundle=bundle, kind="catalog")
                if _catalog_retained_digest(saved) != catalog_digest:
                    raise KernelError("migration_integrity_failed", "目录身份备份核验失败")
        backups = {}
        for row in rows:
            result = create_portable(
                row["path"], backup, request_id=operation, _bundle=sources[row["id"]]
            )
            backups[row["id"]] = result["path"]
        results = []
        for row in rows:
            try:
                with closing(connect(row["path"])) as connection:
                    result = upgrade_company(
                        connection, bundle, fault=fault,
                        expected_source_fingerprint=sources[row["id"]].current("company")["sha256"],
                        expected_company_id=row["id"],
                        expected_database_id=row["database_id"],
                        expected_taxpayer_id=row["taxpayer_id"],
                    )
            except Exception as exc:
                from .diagnostics import error_response

                raise KernelError(
                    "development_upgrade_partial" if results else "development_upgrade_failed",
                    "开发升级未全部完成；失败公司事务已回滚，可按备份和实际前缀接续",
                    backup_directory=str(backup),
                    completed_companies=results,
                    failed_company_id=row["id"],
                    cause_code=error_response(exc)["code"],
                ) from exc
            results.append({"company_id": row["id"], "backup": backups[row["id"]], **result})
        with closing(connect(root / "catalog.sqlite", read_only=True)) as catalog:
            verify_schema(catalog, bundle=bundle, kind="catalog")
            if _catalog_retained_digest(catalog) != catalog_digest:
                raise KernelError("migration_integrity_failed", "开发升级改变了目录或负责人身份")
        return {
            "status": "upgraded",
            "catalog": "verified_unchanged",
            "backup_directory": str(backup),
            "companies": results,
        }
