"""Fixed draft-history adjustment; SQL comes only from shipped exact contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import MappingProxyType

from ai_accounting.kernel.backup import _retained_history_digest, _verify_connection
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.development_contracts import get_contract_sha
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.types import canonical, digest
from ai_accounting.kernel.versions import fingerprint, objects, verify_schema

COMPANY_SOURCE = "0c8abb83f06fa66266ba5802f5d70cabed3b861015a977757e0538f14a8ee2e7"
CATALOG_SOURCE = "b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee"
PRESERVED_BODIES = {
    "fact_payroll": "fact_payroll_pre_revision",
    "fact_payroll_plan_v2": "fact_payroll_plan_v2_pre_revision",
    "fact_payroll_plan_v2_change_notice_revisions": (
        "fact_payroll_plan_v2_pre_revision_change_notice_revisions"
    ),
    "fact_material_resolution_v2": "fact_material_resolution_v2_pre_reference",
    "fact_material_resolution_v2_links": "fact_material_resolution_v2_pre_reference_links",
    "fact_loan_interest": "fact_loan_interest_pre_reference",
}
EMPTY_CHANGED = frozenset(
    {
        "management_revision",
        "display_profile_revision",
        "fact_payroll_bounded",
        "fact_payroll_no_change_v2",
        "fact_payroll_no_change_v2_employees",
        "fact_material_group_resolution_links",
    }
)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _columns(connection, table):
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_info({_quote(table)})"))


def _row_digest(connection, table, columns, *, where="", parameters=()):
    fields = ",".join(map(_quote, columns))
    digest, count = hashlib.sha256(), 0
    for row in connection.execute(
        f"SELECT {fields} FROM {_quote(table)} {where} ORDER BY {fields}",
        parameters,
    ):
        raw = json.dumps(
            [["blob", bytes(v).hex()] if isinstance(v, bytes) else ["value", v] for v in row],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _require(ok, message):
    if not ok:
        raise KernelError("history_adjustment_rejected", message)


def _contracts(kind):
    bundle = production_bundle()
    source = get_contract_sha(bundle, kind, COMPANY_SOURCE if kind == "company" else CATALOG_SOURCE)
    _require(source is not None, "缺少包内精确历史结构来源")
    target = bundle.current(kind)
    _require(source["sha256"] != target["sha256"], "历史调整目标未独立声明")
    _require(
        (source["sha256"], target["sha256"]) in bundle.draft_transitions.get(kind, ()),
        "历史调整来源和目标未在包内声明",
    )
    return bundle, source, target


def source_bundle():
    """Use the exact package-owned source grammar for independent source proof."""
    from ai_accounting.kernel.source24_history import source_registry

    bundle = production_bundle()
    company = get_contract_sha(bundle, "company", COMPANY_SOURCE)
    catalog = get_contract_sha(bundle, "catalog", CATALOG_SOURCE)
    _require(company is not None and catalog is not None, "缺少固定历史来源合同")
    return replace(
        bundle,
        registry=source_registry(bundle.registry),
        contracts=MappingProxyType(
            {
                "company": MappingProxyType({0: company}),
                "catalog": MappingProxyType({0: catalog}),
            }
        ),
    )


def _fund_setting_evidence(connection, accounts, first_period):
    """Preserve a new declared synthetic configuration, never an old owner confirmation."""
    if not accounts:
        return accounts, None
    declaration = {
        "format": "ai-accounting-kernel/2/synthetic-fund-settings/1",
        "meaning": "new synthetic format-upgrade settings; not original owner confirmation",
        "first_period": first_period,
        "accounts": [
            {key: item[key] for key in ("entity_id", "data", "source")} for item in accounts
        ],
    }
    content = canonical(declaration).encode()
    hashed = hashlib.sha256(content).digest()
    media_type, name = "application/json", "合成样本格式升级资金配置"
    key = "synthetic-fund-settings-" + hashed.hex()
    result = {"status": "registered", "digest": hashed.hex()}
    connection.execute(
        "INSERT INTO evidence VALUES(?,?,?,?) ON CONFLICT(digest) DO NOTHING",
        (hashed, content, media_type, name),
    )
    connection.execute("UPDATE state SET material=material+1 WHERE id=1")
    audit = connection.execute(
        "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
        (key, "evidence", canonical(result)),
    )
    from ai_accounting.kernel.read_indexes import sync_audit

    sync_audit(connection, audit.lastrowid)
    connection.execute(
        "INSERT INTO request VALUES(?,?,?)",
        (key, digest(["evidence", hashed.hex(), media_type, name]), canonical(result)),
    )
    return [dict(item, evidence_digest=hashed.hex()) for item in accounts], dict(
        result,
        request_id=key,
    )


def plan_connection(connection, *, kind):
    """Read structure and empty-table guards without constructing a service."""
    _require(kind in {"company", "catalog"}, "未知数据库类别")
    bundle, source, target = _contracts(kind)
    actual = objects(connection)
    actual_sha = fingerprint(actual).hex()
    if actual == target["objects"]:
        verify_schema(connection, bundle=bundle, kind=kind)
        return {"status": "already_adjusted", "kind": kind, "target_fingerprint": actual_sha}
    _require(
        actual == source["objects"] and actual_sha == source["sha256"], "实际SQL不匹配固定历史来源"
    )
    old = {(o["type"], o["name"]): o["sql"] for o in source["objects"]}
    new = {(o["type"], o["name"]): o["sql"] for o in target["objects"]}
    changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    guards = EMPTY_CHANGED if kind == "company" else frozenset({"security_identity_import"})
    counts = {}
    for name in guards | {n for typ, n in old.keys() - new.keys() if typ == "table"}:
        if ("table", name) in old:
            count = connection.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
            _require(count == 0, f"必须保留的非空历史表不能移除: {name}")
            counts[name] = count
    if kind == "company":
        for name, historical in PRESERVED_BODIES.items():
            if ("table", name) not in old:
                continue
            _require(("table", historical) in new, f"目标缺少保留正文表: {historical}")
        allowed_changed = set(PRESERVED_BODIES) | set(EMPTY_CHANGED) | {"source_change"}
        _require(
            all(
                n in allowed_changed
                for typ, n in changed
                if typ == "table" and (typ, n) in old and (typ, n) in new
            ),
            "未声明的非空正文结构变化",
        )
    return {
        "status": "planned",
        "kind": kind,
        "source_fingerprint": actual_sha,
        "target_fingerprint": target["sha256"],
        "empty_guards": counts,
        "changed_objects": [{"type": typ, "name": name} for typ, name in changed],
        "preserved_body_tables": PRESERVED_BODIES if kind == "company" else {},
    }


def capture_source_guard(connection, *, kind):
    """Capture a single caller-owned read snapshot for backup-to-write binding."""
    _require(connection.in_transaction, "来源守卫必须在单一事务快照中采集")
    plan_connection(connection, kind=kind)
    schema = objects(connection)
    original = {}
    for item in schema:
        if item["type"] == "table":
            name = item["name"]
            columns = _columns(connection, name)
            original[name] = (columns, _row_digest(connection, name, columns))
    return {"schema": schema, "tables": original}


def adjust_connection(
    connection, *, kind, fault=None, fund_accounts=None, first_fund_period=None,
    expected_source_guard=None,
):
    """One-file atomic DDL, original-row comparison and production verification."""
    _require(not connection.in_transaction, "历史调整必须从无事务连接开始")
    plan = plan_connection(connection, kind=kind)
    if plan["status"] == "already_adjusted":
        return plan
    bundle, source, target = _contracts(kind)
    old = {(o["type"], o["name"]): o["sql"] for o in source["objects"]}
    new = {(o["type"], o["name"]): o["sql"] for o in target["objects"]}
    from ai_accounting.kernel.runtime import migration_write

    with migration_write(
        connection, foreign_keys_off=True,
        before_restore=(lambda: fault("before_restore_foreign_keys")) if fault else None,
    ) as transaction:
        _require(objects(connection) == source["objects"], "锁定后历史结构发生变化")
        # Recheck guards after acquiring the write lock.
        plan_connection(connection, kind=kind)
        guard = capture_source_guard(connection, kind=kind)
        if expected_source_guard is not None:
            _require(guard == expected_source_guard, "备份来源快照在锁定前发生变化")
        original = guard["tables"]
        retained = (
            _retained_history_digest(connection)
            if kind == "company"
            else hashlib.sha256(
                json.dumps({n: v[1] for n, v in original.items()}, sort_keys=True).encode()
            ).hexdigest()
        )
        coverage = None
        if kind == "company":
            from ai_accounting.kernel.change_journal import capture_retained_coverage

            coverage = capture_retained_coverage(
                connection,
                source_fingerprint=source["sha256"],
                retained_history_digest=retained,
            )
        for typ, name in old:
            if typ in {"index", "trigger", "view"} and old[(typ, name)] != new.get((typ, name)):
                connection.execute(f"DROP {typ.upper()} {_quote(name)}")
        preserved = PRESERVED_BODIES if kind == "company" else {}
        for name, historical in preserved.items():
            if ("table", name) not in old:
                continue
            connection.execute(new[("table", historical)])
            fields = ",".join(map(_quote, original[name][0]))
            _require(
                _columns(connection, historical) == original[name][0],
                f"历史正文字段并非原样保留: {name}",
            )
            connection.execute(
                f"INSERT INTO {_quote(historical)}({fields}) SELECT {fields} FROM {_quote(name)}"
            )
        if fault:
            fault("after_history_copy")
        rebuild = {
            n
            for typ, n in old
            if typ == "table" and (old[(typ, n)] != new.get((typ, n)) or n in preserved)
        }
        for name in rebuild:
            if name not in preserved and ("table", name) in new and original[name][1]["rows"]:
                _require(name == "source_change", f"非空表缺少明确迁移规则: {name}")
                connection.execute(
                    f"CREATE TEMP TABLE adjustment_source_change AS SELECT * FROM {_quote(name)}"
                )
            connection.execute(f"DROP TABLE {_quote(name)}")
        if fault:
            fault("after_drop")
        present = {(o["type"], o["name"]) for o in objects(connection)}
        for o in target["objects"]:
            if o["type"] == "table" and (o["type"], o["name"]) not in present:
                connection.execute(o["sql"])
        if "source_change" in rebuild:
            fields = ",".join(map(_quote, original["source_change"][0]))
            if original["source_change"][1]["rows"]:
                connection.execute(
                    f"INSERT INTO source_change({fields}) "
                    f"SELECT {fields} FROM adjustment_source_change"
                )
                connection.execute("DROP TABLE adjustment_source_change")
        present = {(o["type"], o["name"]) for o in objects(connection)}
        for o in target["objects"]:
            if o["type"] != "table" and (o["type"], o["name"]) not in present:
                connection.execute(o["sql"])
        _require(objects(connection) == target["objects"], "调整后的SQL不等于完整目标合同")
        for name, (columns, before) in original.items():
            destination = preserved.get(name, name)
            if before["rows"] == 0 and (
                ("table", destination) not in new or _columns(connection, destination) != columns
            ):
                continue
            _require(
                _row_digest(connection, destination, columns) == before, f"调整改变原始记录: {name}"
            )
        if kind == "company":
            _require(_retained_history_digest(connection) == retained, "冻结、凭证或审计历史改变")
            from ai_accounting.kernel.change_journal import install_retained_coverage

            install_retained_coverage(connection, coverage)
        sequence = connection.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM schema_draft_history"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO schema_draft_history(sequence,source_fingerprint,target_fingerprint,"
            "retained_history_digest) VALUES(?,?,?,?)",
            (
                sequence,
                bytes.fromhex(source["sha256"]),
                bytes.fromhex(target["sha256"]),
                bytes.fromhex(retained),
            ),
        )
        verify_schema(connection, bundle=bundle, kind=kind)
        metadata_evidence = None
        metadata_state_delta = None
        if kind == "company":
            from ai_accounting.kernel.offline_development_upgrade import _initialize_fund_details

            state_before = dict(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
            initialized_accounts, metadata_evidence = _fund_setting_evidence(
                connection,
                fund_accounts,
                first_fund_period,
            )
            _initialize_fund_details(connection, bundle, initialized_accounts, first_fund_period)
            state_after = dict(connection.execute("SELECT * FROM state WHERE id=1").fetchone())
            metadata_state_delta = {
                key: state_after[key] - value for key, value in state_before.items()
            }
            _require(
                all(
                    value == (1 if key == "material" and metadata_evidence else 0)
                    for key, value in metadata_state_delta.items()
                ),
                "新增合成管理来源推进了不适用状态",
            )
            protected = {
                name
                for name in original
                if name.startswith(
                    (
                        "fact_",
                        "calculation",
                        "dependency_",
                        "voucher",
                        "close_",
                    )
                )
                or name
                in {
                    "identity",
                    "subject",
                    "period_close",
                    "entity",
                    "entity_profile_revision",
                }
            }
            for name in protected:
                destination = preserved.get(name, name)
                columns, before = original[name]
                if not before["rows"] and (
                    ("table", destination) not in new
                    or _columns(connection, destination) != columns
                ):
                    continue
                _require(
                    _row_digest(connection, destination, columns) == before,
                    f"追加管理来源改变了受保护原始数据: {name}",
                )
            if metadata_evidence is not None:
                filters = {
                    "evidence": ("WHERE digest<>?", (bytes.fromhex(metadata_evidence["digest"]),)),
                    "audit": ("WHERE request_id<>?", (metadata_evidence["request_id"],)),
                    "request": ("WHERE id<>?", (metadata_evidence["request_id"],)),
                    "source_change": ("WHERE sequence<=?", (coverage["through_sequence"],)),
                }
                for name, (where, values) in filters.items():
                    columns, before = original[name]
                    _require(
                        _row_digest(connection, name, columns, where=where, parameters=values)
                        == before,
                        f"追加来源改变了原记录: {name}",
                    )
            if fault:
                fault("after_fund_initialization")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise KernelError("migration_integrity_failed", "历史调整后的外键核验失败")
        verification = None
        if kind == "company":
            verification = _verify_connection(connection, bundle, allow_previous=False)[
                "verification"
            ]
            _require(
                verification["status"] == "verified" and not verification["limitations"],
                "目标生产核验未通过",
            )
        if fault:
            fault("before_commit")
        transaction["result"] = {
            **plan,
            "status": "adjusted",
            "retained_history_digest": retained,
            "original_tables": {n: v[1] for n, v in original.items()},
            "verification": verification,
            "new_synthetic_metadata_evidence": metadata_evidence,
            "synthetic_metadata_state_delta": metadata_state_delta,
        }
        return transaction["result"]
