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
    _retained_history_digest,
    _verify_connection,
    copy_to_unpublished_database,
    create_portable,
    verify_file,
)
from .contracts import KernelError
from .development_contracts import (
    HISTORY_PRIOR_FINGERPRINT,
    HISTORY_TARGET_FINGERPRINT,
    REMUNERATION_SOURCE_FINGERPRINT,
    REMUNERATION_TARGET_FINGERPRINT,
    get_contract_sha,
    remuneration_source_registry,
)
from .migration_contracts import diff_contracts
from .offline_upgrade import _catalog_retained_digest, _catalog_rows
from .permissions import create_private_file, ensure_private_directory, reject_reparse_path
from .runtime import connect, private_file_lock, verification_snapshot
from .schema_bundle import production_bundle
from .versions import fingerprint, objects, verify_schema

SOURCE_FINGERPRINT = "923584f720781cb28a369dd5b269537f64a4551034d306e9447bc2935d263861"
INDEX_SOURCE_FINGERPRINT = "c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb"
INDEX_TARGET_FINGERPRINT = "52556e8ec6cbbb81ab9ad9a69bb87897401471dba9c693e7b7ebddd2a5b08bf4"
READ_MODEL_SOURCE_FINGERPRINT = INDEX_TARGET_FINGERPRINT
READ_MODEL_TARGET_FINGERPRINT = "0c8abb83f06fa66266ba5802f5d70cabed3b861015a977757e0538f14a8ee2e7"
FUND_DETAIL_SOURCE_FINGERPRINT = READ_MODEL_TARGET_FINGERPRINT
FUND_DETAIL_TARGET_FINGERPRINT = "cdbdfa36c6513d7c2168edbeb2780692efe883ce7e5b59b63e87702288d79efa"
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
    if source is None or source_fingerprint not in (
        SOURCE_FINGERPRINT,
        INDEX_SOURCE_FINGERPRINT,
        READ_MODEL_SOURCE_FINGERPRINT,
        FUND_DETAIL_SOURCE_FINGERPRINT,
        REMUNERATION_SOURCE_FINGERPRINT,
        REMUNERATION_TARGET_FINGERPRINT,
        HISTORY_PRIOR_FINGERPRINT,
    ):
        raise KernelError("migration_not_declared", "开发升级来源未明确声明")
    if source_fingerprint == REMUNERATION_SOURCE_FINGERPRINT:
        return replace(
            bundle,
            registry=remuneration_source_registry(bundle.registry),
            contracts=MappingProxyType(
                {**bundle.contracts, "company": MappingProxyType({0: source})}
            ),
        )
    if source_fingerprint in {REMUNERATION_TARGET_FINGERPRINT, HISTORY_PRIOR_FINGERPRINT}:
        from .contracts import Registry

        tables = {item["name"] for item in source["objects"] if item["type"] == "table"}
        registry = Registry()
        for kind in bundle.registry.models:
            bodies = tuple((model, table) for model, table in bundle.registry.fact_bodies(kind)
                           if table in tables)
            model, table = bodies[0]
            registry.register(model, bundle.registry.evaluators.get(kind),
                              table=table, historical=bodies[1:])
        registry.historical_only = bundle.registry.historical_only
        registry.accounting_projectors = dict(bundle.registry.accounting_projectors)
        registry.accounting_consumers = dict(bundle.registry.accounting_consumers)
        registry.readiness = dict(bundle.registry.readiness)
        registry.snapshot_readiness = dict(bundle.registry.snapshot_readiness)
        return replace(bundle, registry=registry, contracts=MappingProxyType(
            {**bundle.contracts, "company": MappingProxyType({0: source})}
        ))
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
    if REMUNERATION_SOURCE_FINGERPRINT in bundle.development_contracts.get("company", {}):
        return _remuneration_changes(bundle)
    source = get_contract_sha(bundle, "company", SOURCE_FINGERPRINT)
    intermediate = get_contract_sha(bundle, "company", INDEX_SOURCE_FINGERPRINT)
    read_model_source = get_contract_sha(bundle, "company", READ_MODEL_SOURCE_FINGERPRINT)
    fund_source = get_contract_sha(bundle, "company", FUND_DETAIL_SOURCE_FINGERPRINT)
    target = bundle.current("company")
    transitions = (
        (SOURCE_FINGERPRINT, INDEX_SOURCE_FINGERPRINT),
        (INDEX_SOURCE_FINGERPRINT, INDEX_TARGET_FINGERPRINT),
        (READ_MODEL_SOURCE_FINGERPRINT, READ_MODEL_TARGET_FINGERPRINT),
        (FUND_DETAIL_SOURCE_FINGERPRINT, FUND_DETAIL_TARGET_FINGERPRINT),
    )
    if (
        bundle.status != "draft"
        or source is None
        or intermediate is None
        or read_model_source is None
        or fund_source is None
        or target["sha256"] != FUND_DETAIL_TARGET_FINGERPRINT
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
    index_differences = diff_contracts(intermediate, read_model_source)
    if index_differences != [
        {
            "type": "index",
            "name": "close_reference_direct_adoption",
            "old_sql": None,
            "new_sql": _DIRECT_ADOPTION_SQL,
        }
    ]:
        raise KernelError("migration_not_declared", "直接采用索引迁移与精确声明不一致")
    from .dashboard_read_model import DASHBOARD_READ_MODEL_DDL
    from .versions import contract

    model_differences = diff_contracts(read_model_source, fund_source)
    expected_models = [
        {"type": item["type"], "name": item["name"], "old_sql": None, "new_sql": item["sql"]}
        for item in contract(DASHBOARD_READ_MODEL_DDL)
    ]
    if model_differences != expected_models:
        raise KernelError("migration_not_declared", "看板读取结构迁移与逐对象精确声明不一致")
    from .fund_accounts import FUND_ACCOUNTS_DDL
    from .fund_line_details import FUND_LINE_DETAILS_DDL

    fund_differences = diff_contracts(fund_source, target)
    expected_funds = [
        {"type": item["type"], "name": item["name"], "old_sql": None, "new_sql": item["sql"]}
        for item in contract(FUND_ACCOUNTS_DDL + FUND_LINE_DETAILS_DDL)
    ]
    if fund_differences != expected_funds:
        raise KernelError("migration_not_declared", "资金明细迁移与逐对象精确声明不一致")
    return (
        (source, intermediate, differences),
        (intermediate, read_model_source, index_differences),
        (read_model_source, fund_source, model_differences),
        (fund_source, target, fund_differences),
    )


def _remuneration_changes(bundle):
    """Only new typed body tables and their exact one-body seal declaration."""
    from .contracts import Registry
    from .schema import fact_ddl
    from .versions import contract

    source = get_contract_sha(bundle, "company", REMUNERATION_SOURCE_FINGERPRINT)
    target = get_contract_sha(bundle, "company", REMUNERATION_TARGET_FINGERPRINT)
    transition = (REMUNERATION_SOURCE_FINGERPRINT, REMUNERATION_TARGET_FINGERPRINT)
    if (
        bundle.status != "draft" or source is None
        or target["sha256"] != REMUNERATION_TARGET_FINGERPRINT
        or transition not in bundle.draft_transitions.get("company", ())
    ):
        raise KernelError("migration_not_declared", "实际税额结构调整须有精确来源和目标")
    changed = {
        "payroll", "payroll_bounded", "payroll_profile", "annual_bonus", "labor",
        "tax_import_details_v2",
    }
    added = Registry()
    for kind in sorted(changed | {"remuneration_tax_basis"}):
        if kind not in bundle.registry.models:
            raise KernelError("migration_not_declared", "实际税额结构调整缺少当前正文类型")
        model = bundle.registry.models[kind]
        table = bundle.registry.current_table(kind)
        if kind in changed and table != "fact_" + kind + "_confirmed":
            raise KernelError("migration_not_declared", "当前正文必须使用独立保留表")
        added.register(model, table=table)
    expected = {
        (item["type"], item["name"]): {"type": item["type"], "name": item["name"],
                                      "old_sql": None, "new_sql": item["sql"]}
        for item in contract(fact_ddl(added))
    }
    old_seal = next(item["sql"] for item in source["objects"] if item["name"] == "fact_seal_shape")
    new_seal = next(
        item["sql"] for item in target["objects"]
        if item["name"] == "fact_seal_shape"
    )
    expected[("trigger", "fact_seal_shape")] = {
        "type": "trigger", "name": "fact_seal_shape", "old_sql": old_seal, "new_sql": new_seal,
    }
    differences = diff_contracts(source, target)
    if differences != [expected[key] for key in sorted(expected)]:
        raise KernelError("migration_not_declared", "实际税额结构调整超出新增正文和封存检查")
    return ((source, target, differences), _history_body_changes(bundle),
            _prior_history_changes(bundle))



def _history_body_changes(bundle):
    """Add the four exact read-only bodies and journal retention tables to e7."""
    from .contracts import Registry
    from .schema import fact_ddl
    from .source24_history import HISTORICAL_BODIES
    from .versions import contract

    source = get_contract_sha(bundle, "company", REMUNERATION_TARGET_FINGERPRINT)
    target = bundle.current("company")
    if (source is None or target["sha256"] != HISTORY_TARGET_FINGERPRINT
        or (source["sha256"], target["sha256"]) not in bundle.draft_transitions["company"]):
        raise KernelError("migration_not_declared", "历史正文增加缺少精确声明")
    added = Registry()
    for model, table in HISTORICAL_BODIES.values():
        added.register(model, table=table)
    from .change_journal import retained_coverage_ddl
    from .materials import MATERIAL_HISTORY_INDEX_DDL

    expected = {(o["type"], o["name"]): {"type": o["type"], "name": o["name"],
                 "old_sql": None, "new_sql": o["sql"]}
                for o in contract(
                    fact_ddl(added) + retained_coverage_ddl() + MATERIAL_HISTORY_INDEX_DDL
                )}
    # Both fingerprints are package-owned and the seal changes only to include
    # these extra bodies. Validate its exact generated target text independently.
    from .schema import schema_sql

    seal = next(o["sql"] for o in contract(schema_sql(bundle.registry))
                if o["name"] == "fact_seal_shape")
    before = next(o["sql"] for o in source["objects"] if o["name"] == "fact_seal_shape")
    expected[("trigger", "fact_seal_shape")] = dict(type="trigger", name="fact_seal_shape",
                                                     old_sql=before, new_sql=seal)
    differences = diff_contracts(source, target)
    if differences != [expected[key] for key in sorted(expected)]:
        raise KernelError("migration_not_declared", "历史增加超出精确正文及封存声明")
    return source, target, differences


def _prior_history_changes(bundle):
    from .materials import MATERIAL_HISTORY_SOURCE_INDEX_DDL
    from .versions import contract

    source = get_contract_sha(bundle, "company", HISTORY_PRIOR_FINGERPRINT)
    target = bundle.current("company")
    parent_sql = next(o["sql"] for o in source["objects"]
                      if o["type"] == "table"
                      and o["name"] == "fact_material_resolution_v2_pre_reference")
    # An index's saved SQL needs its declared table when executed by the
    # independent SQLite contract builder. Do not invent a reduced table.
    expected = [dict(type=o["type"], name=o["name"], old_sql=None, new_sql=o["sql"])
                for o in contract(parent_sql + ";\n" + MATERIAL_HISTORY_SOURCE_INDEX_DDL)
                if o["type"] == "index"]
    differences = diff_contracts(source, target)
    if (differences != expected
        or (source["sha256"], target["sha256"]) not in bundle.draft_transitions["company"]):
        raise KernelError("migration_not_declared", "已调整历史结构仅增加精确来源索引")
    return source, target, differences

def _apply_remuneration_step(connection, differences, *, fault=None):
    for kind in ("table", "index", "trigger"):
        for item in differences:
            if item["type"] != kind:
                continue
            if item["name"] == "fact_seal_shape":
                connection.execute("DROP TRIGGER fact_seal_shape")
            connection.execute(item["new_sql"])
            if fault:
                fault("after_remuneration_object:" + kind + ":" + item["name"])


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


def _apply_read_model_step(connection, differences, *, fault=None):
    """Add only authenticated derived storage, leaving every new table empty."""
    for kind in ("table", "index", "trigger"):
        for item in differences:
            if item["type"] == kind:
                connection.execute(item["new_sql"])
                if fault:
                    fault("after_read_model_object:" + kind + ":" + item["name"])


def _initialize_fund_details(connection, bundle, accounts, first_period):
    from .contracts import NeedsInformation
    from .engine import Engine
    from .fund_accounts import identity_issue, install_fund_account_definition
    from .fund_line_details import initialize_open_details
    from .storage import Store
    from .types import YearMonth

    entities = {
        row["id"]: row["account_type"]
        for row in connection.execute(
            "SELECT id,account_type FROM entity WHERE kind='fund_account'"
        )
    }
    provided = {}
    for item in accounts or ():
        if not isinstance(item, dict) or item.get("entity_id") in provided:
            raise KernelError("invalid_command", "资金账户初始化资料重复或无效")
        provided[item.get("entity_id")] = item
    if set(provided) != set(entities):
        raise NeedsInformation(
            [identity_issue("fund_accounts", "现有受管资金账户必须逐一提供有来源的完整身份资料")]
        )
    for entity_id, account_type in sorted(entities.items()):
        item = provided[entity_id]
        data = item.get("data", {})
        install_fund_account_definition(
            connection,
            entity_id,
            account_type,
            data,
            source=item.get("source"),
            evidence_digest=item.get("evidence_digest"),
        )
    if first_period is None:
        previous = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
        first = (
            previous + 1
            if previous is not None
            else connection.execute("SELECT min(period) FROM fact_revision").fetchone()[0]
        )
        if first is None:
            return
        first_period = str(YearMonth.from_ordinal(first))
    first_period = YearMonth(first_period)
    identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
    path = connection.execute("PRAGMA database_list").fetchone()[2]
    engine = Engine(Store(path, bundle, identity["company_id"], identity["database_id"]))
    initialize_open_details(engine, connection, first_period)


def upgrade_company(
    connection,
    bundle,
    *,
    fault=None,
    expected_source_fingerprint=None,
    expected_company_id=None,
    expected_database_id=None,
    expected_taxpayer_id=None,
    fund_accounts=None,
    first_fund_period=None,
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
        if (
            expected_source_fingerprint is not None
            and previous.current("company")["sha256"] != expected_source_fingerprint
        ):
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
    steps = []
    path_fingerprint = source["sha256"]
    while path_fingerprint != target["sha256"]:
        matches = [step for step in declared if step[0]["sha256"] == path_fingerprint]
        if len(matches) != 1 or len(steps) >= len(declared):
            raise KernelError("migration_not_declared", "开发升级路径不连续或不唯一")
        step = matches[0]
        steps.append(step)
        path_fingerprint = step[1]["sha256"]
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
            step_rows = (
                original_rows if step_index == 0 else _all_existing_rows(connection, step_source)
            )
            if step_source["sha256"] in {
                REMUNERATION_SOURCE_FINGERPRINT, REMUNERATION_TARGET_FINGERPRINT,
            }:
                _apply_remuneration_step(connection, differences, fault=fault)
            elif step_source["sha256"] == SOURCE_FINGERPRINT:
                _apply_first_step(connection, step_target, differences, fault=fault)
            elif step_source["sha256"] == INDEX_SOURCE_FINGERPRINT:
                connection.execute(differences[0]["new_sql"])
            else:
                _apply_read_model_step(connection, differences, fault=fault)
                if step_source["sha256"] == FUND_DETAIL_SOURCE_FINGERPRINT:
                    _initialize_fund_details(connection, bundle, fund_accounts, first_fund_period)
            if fault:
                fault(
                    "after_ddl"
                    if step_source["sha256"] == SOURCE_FINGERPRINT
                    else "after_index_ddl"
                    if step_source["sha256"] == INDEX_SOURCE_FINGERPRINT
                    else "after_read_model_ddl"
                )
            if (
                objects(connection) != step_target["objects"]
                or fingerprint(objects(connection)).hex() != step_target["sha256"]
            ):
                raise KernelError("schema_fingerprint_mismatch", "开发升级未生成精确目标结构")
            if (
                _all_existing_rows(connection, step_source) != step_rows
                or _retained_history_digest(connection) != retained
            ):
                raise KernelError("migration_integrity_failed", "开发升级改变了原始数据或保留历史")
            sequence = connection.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM schema_draft_history"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
                "retained_history_digest) VALUES(?,?,?,?)",
                (
                    sequence,
                    bytes.fromhex(step_source["sha256"]),
                    bytes.fromhex(step_target["sha256"]),
                    bytes.fromhex(retained),
                ),
            )
            step_bundle = (
                bundle if step_target is target else source_bundle(bundle, step_target["sha256"])
            )
            verify_schema(connection, bundle=step_bundle)
            applied.append(
                {
                    "sequence": sequence,
                    "source_fingerprint": step_source["sha256"],
                    "target_fingerprint": step_target["sha256"],
                }
            )
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
    if actual in (
        SOURCE_FINGERPRINT,
        INDEX_SOURCE_FINGERPRINT,
        READ_MODEL_SOURCE_FINGERPRINT,
        FUND_DETAIL_SOURCE_FINGERPRINT,
        REMUNERATION_SOURCE_FINGERPRINT,
        REMUNERATION_TARGET_FINGERPRINT,
        HISTORY_PRIOR_FINGERPRINT,
    ):
        previous = source_bundle(bundle, actual)
        verify_schema(connection, bundle=previous)
        return previous
    raise KernelError("schema_fingerprint_mismatch", "当前库不属于本次开发升级的精确来源或目标")



def _catalog_bundle(connection, bundle):
    from .development_contracts import CATALOG_PRIOR_FINGERPRINT

    actual = fingerprint(objects(connection)).hex()
    if actual == bundle.current("catalog")["sha256"]:
        previous = bundle
    elif actual == CATALOG_PRIOR_FINGERPRINT:
        source = get_contract_sha(bundle, "catalog", actual)
        previous = replace(bundle, contracts=MappingProxyType(
            {**bundle.contracts, "catalog": MappingProxyType({0: source})}
        ))
    else:
        raise KernelError("schema_fingerprint_mismatch", "目录不属于本次明确加表来源")
    verify_schema(connection, bundle=previous, kind="catalog")
    return previous


def _upgrade_catalog(connection, bundle, *, fault=None):
    """The exact 0ec catalog gains append-only draft receipts, preserving all rows."""
    from .versions import DRAFT_HISTORY_DDL, contract

    if connection.in_transaction:
        raise KernelError("migration_transaction_active", "目录升级须从无事务连接开始")
    previous = _catalog_bundle(connection, bundle)
    if previous is bundle:
        return "verified_skip"
    source, target = previous.current("catalog"), bundle.current("catalog")
    differences = diff_contracts(source, target)
    expected = [dict(type=o["type"], name=o["name"], old_sql=None, new_sql=o["sql"])
                for o in contract(DRAFT_HISTORY_DDL)]
    if (differences != expected
        or (source["sha256"], target["sha256"]) not in bundle.draft_transitions["catalog"]):
        raise KernelError("migration_not_declared", "目录调整超出精确追加履历声明")
    try:
        connection.execute("BEGIN IMMEDIATE")
        verify_schema(connection, bundle=previous, kind="catalog")
        rows = _all_existing_rows(connection, source)
        retained = _catalog_retained_digest(connection)
        _apply_read_model_step(connection, differences, fault=fault)
        if _all_existing_rows(connection, source) != rows:
            raise KernelError("migration_integrity_failed", "目录升级改变原始记录")
        connection.execute(
            "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
            "retained_history_digest) VALUES(1,?,?,?)",
            (bytes.fromhex(source["sha256"]), bytes.fromhex(target["sha256"]),
             bytes.fromhex(retained)),
        )
        verify_schema(connection, bundle=bundle, kind="catalog")
        if fault:
            fault("before_catalog_commit")
        connection.commit()
        return "upgraded"
    except BaseException:
        connection.rollback()
        raise

def upgrade_root(root, *, bundle=None, fault=None, fund_account_inputs=None):
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
            catalog_source = _catalog_bundle(catalog, bundle)
            _, rows = _catalog_rows(
                catalog, root, catalog_source, allow_previous=False, require_completed=True
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
                verify_schema(saved, bundle=catalog_source, kind="catalog")
                if _catalog_retained_digest(saved) != catalog_digest:
                    raise KernelError("migration_integrity_failed", "目录身份备份核验失败")
        backups = {}
        for row in rows:
            result = create_portable(
                row["path"], backup, request_id=operation, _bundle=sources[row["id"]]
            )
            backups[row["id"]] = result["path"]
        with closing(connect(root / "catalog.sqlite")) as catalog:
            catalog_result = _upgrade_catalog(catalog, bundle, fault=fault)
        results = []
        for row in rows:
            try:
                with closing(connect(row["path"])) as connection:
                    result = upgrade_company(
                        connection,
                        bundle,
                        fault=fault,
                        expected_source_fingerprint=sources[row["id"]].current("company")["sha256"],
                        expected_company_id=row["id"],
                        expected_database_id=row["database_id"],
                        expected_taxpayer_id=row["taxpayer_id"],
                        fund_accounts=(fund_account_inputs or {})
                        .get(row["id"], {})
                        .get("accounts"),
                        first_fund_period=(fund_account_inputs or {})
                        .get(row["id"], {})
                        .get("first_period"),
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
            "catalog": catalog_result,
            "backup_directory": str(backup),
            "companies": results,
        }
