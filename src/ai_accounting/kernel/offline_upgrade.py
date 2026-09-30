"""Offline, package-owned upgrades of one registered accounting root.

No catalog constructor or service client is used here: both can create or resume
work while a root still belongs to an older installed release.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from pathlib import Path

from .backup import _retained_history_digest, _verify_connection, verify_file
from .contracts import KernelError
from .permissions import reject_reparse_path
from .runtime import connect, private_file_lock
from .schema_bundle import production_bundle
from .versions import _migration_plan, verify_schema
from .versions import upgrade as upgrade_database


def _catalog_rows(connection, root, bundle, *, allow_previous, require_completed):
    version = verify_schema(
        connection, bundle=bundle, kind="catalog", allow_previous=allow_previous
    )
    if require_completed and connection.execute(
        "SELECT 1 FROM company_operation WHERE status!='succeeded' LIMIT 1"
    ).fetchone():
        raise KernelError(
            "company_operation_incomplete",
            "目录中存在未完成的创建或恢复；请先用当前安装版处理",
        )
    if any(row[0] != "ok" for row in connection.execute("PRAGMA integrity_check")):
        raise KernelError("catalog_content_invalid", "目录库完整性检查失败")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise KernelError("catalog_content_invalid", "目录库引用检查失败")
    rows = [dict(row) for row in connection.execute("SELECT * FROM company ORDER BY id")]
    for row in rows:
        expected = root / row["taxpayer_id"] / "company.sqlite"
        if (
            not isinstance(row["taxpayer_id"], str)
            or re.fullmatch(r"[0-9A-Z]{18}", row["taxpayer_id"]) is None
            or not isinstance(row["path"], str)
            or reject_reparse_path(row["path"]) != expected
        ):
            raise KernelError("company_path_mismatch", "登记公司路径与资料根目录不一致")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in (
            "id", "database_id", "taxpayer_id", "name"
        )):
            raise KernelError("company_mismatch", "目录中的公司身份不完整")
    return version, rows


def _company_history_state(connection, result):
    """Compare old immutable columns across one company's upgrade transaction."""
    verification = result["verification"]
    if verification.get("status") != "verified" or verification.get("limitations"):
        raise KernelError("migration_integrity_failed", "离线升级来源内容未完整核验")
    counts = verification["counts"]
    return (
        result["identity"],
        result["evidence_count"],
        result["latest_closed_period"],
        tuple(counts[key] for key in ("facts", "calculations", "vouchers", "closes", "evidence")),
        _retained_history_digest(connection),
    )


def _require_company_history(connection, result, expected):
    if _company_history_state(connection, result) != expected:
        raise KernelError("migration_integrity_failed", "离线升级改变了保留的历史来源")


# A later release that intentionally transforms one of these values needs an
# explicit source-to-target migration proof; an unchanged schema alone cannot
# justify rewriting directory identity, settings or security history.
_CATALOG_RETAINED_ROWS = (
    ("catalog_identity", "id,instance_id", "id"),
    ("company", "id,taxpayer_id,name,path,database_id", "id"),
    (
        "company_operation",
        "id,taxpayer_id,kind,payload,status,attempts,last_error",
        "id",
    ),
    ("company_setting", "company_id,revision,backup_directory", "company_id,revision"),
    (
        "security_owner",
        "id,singleton,login_name,login_key,status,password_hash,credential_version,"
        "password_failures,password_blocked_until,recovery_failures,recovery_blocked_until,"
        "created_at,password_changed_at,last_authenticated_at",
        "id",
    ),
    (
        "security_session",
        "id,owner_id,secret_hash,credential_version,created_at,last_seen_at,"
        "idle_expires_at,absolute_expires_at,revoked_at,revoke_reason",
        "id",
    ),
    (
        "security_recovery",
        "id,owner_id,code_hash,credential_version,created_at,used_at,invalidated_at",
        "id",
    ),
    (
        "security_audit",
        "id,occurred_at,owner_id,session_id,event,outcome,reason,request_id",
        "id",
    ),
    ("security_identity_import", "source_catalog_id,owner_id,imported_at", "source_catalog_id"),
)


def _catalog_retained_digest(connection):
    """Stream the original directory and security columns, excluding version metadata."""
    hashed = hashlib.sha256()
    for table, columns, order in _CATALOG_RETAINED_ROWS:
        hashed.update(table.encode("ascii") + b"\0")
        for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY {order}"):
            values = [
                ["blob", bytes(value).hex()] if isinstance(value, bytes) else ["value", value]
                for value in row
            ]
            encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()
            hashed.update(len(encoded).to_bytes(8, "big"))
            hashed.update(encoded)
    return hashed.hexdigest()


def _inspect_root(root, bundle, *, require_current, check_content, require_completed):
    root = reject_reparse_path(root)
    path = root / "catalog.sqlite"
    if not path.is_file():
        raise KernelError("database_root_unrecognized", "离线升级需要已有正式目录库")
    if bundle.status != "released":
        raise KernelError("schema_version_unsupported", "开发合同不提供正式离线升级")
    with closing(connect(path, read_only=True, validator=lambda c: verify_schema(
        c, bundle=bundle, kind="catalog", allow_previous=not require_current
    ))) as connection:
        connection.execute("BEGIN")
        try:
            catalog_version, rows = _catalog_rows(
                connection, root, bundle,
                allow_previous=not require_current,
                require_completed=require_completed,
            )
        finally:
            connection.rollback()
    if not require_current and catalog_version != bundle.current_versions["catalog"]:
        _migration_plan(
            bundle, "catalog", bundle.contracts["catalog"][catalog_version],
            bundle.current("catalog"),
        )
    versions = {}
    for row in rows:
        company_path = Path(row["path"])
        if not company_path.is_file():
            raise KernelError("company_missing", "已登记的公司数据库不存在")
        if check_content:
            result = verify_file(
                company_path,
                expected_company_id=row["id"],
                expected_database_id=row["database_id"],
                expected_taxpayer_id=row["taxpayer_id"],
                _bundle=bundle,
                _allow_previous=not require_current,
            )
            version = result["database_format"]["version"]
        else:
            with closing(connect(company_path, read_only=True, validator=lambda c: verify_schema(
                c, bundle=bundle, kind="company", allow_previous=not require_current
            ))) as connection:
                connection.execute("BEGIN")
                try:
                    version = verify_schema(
                        connection, bundle=bundle, kind="company",
                        allow_previous=not require_current,
                    )
                    identity = connection.execute(
                        "SELECT company_id,database_id,taxpayer_id FROM identity WHERE id=1"
                    ).fetchone()
                    if identity is None or tuple(identity) != (
                        row["id"], row["database_id"], row["taxpayer_id"]
                    ):
                        raise KernelError("company_mismatch", "公司数据库身份与目录登记不一致")
                finally:
                    connection.rollback()
        versions[row["id"]] = version
        if not require_current and version != bundle.current_versions["company"]:
            _migration_plan(
                bundle, "company", bundle.contracts["company"][version],
                bundle.current("company"),
            )
    return catalog_version, rows, versions


def check_root_current(root, *, bundle=None):
    """Cold-start structure and identity gate; never scans company content."""
    bundle = bundle or production_bundle()
    if bundle.status != "released":
        # The development baseline is still checked by its normal catalog path.
        return
    _inspect_root(
        root, bundle, require_current=True, check_content=False,
        require_completed=False,
    )


def upgrade_root(root, *, bundle=None, fault=None):
    """Upgrade registered companies one by one, then the catalog, under the resident lock."""
    bundle = bundle or production_bundle()
    root = reject_reparse_path(root)
    _inspect_root(
        root, bundle, require_current=False, check_content=True,
        require_completed=True,
    )
    with private_file_lock(root / ".resident.lock") as acquired:
        if not acquired:
            raise KernelError(
                "service_active", "本资料目录的服务正在运行；请用当前安装版 stop 后重试"
            )
        catalog_version, rows, versions = _inspect_root(
            root, bundle, require_current=False, check_content=True,
            require_completed=True,
        )
        results = []
        for row in rows:
            source_version = versions[row["id"]]
            target_version = bundle.current_versions["company"]
            if source_version == target_version:
                results.append({"company_id": row["id"], "status": "verified_skip"})
                continue
            path = Path(row["path"])
            with closing(connect(path, validator=lambda c: verify_schema(
                c, bundle=bundle, kind="company", allow_previous=True
            ))) as connection:
                source_history = []

                def verify_source(candidate, bound_row=row, history=source_history):
                    verified = _verify_connection(
                        candidate, bundle, allow_previous=True,
                        expected_company_id=bound_row["id"],
                        expected_database_id=bound_row["database_id"],
                        expected_taxpayer_id=bound_row["taxpayer_id"],
                    )
                    if history:
                        _require_company_history(candidate, verified, history[0])
                    else:
                        history.append(_company_history_state(candidate, verified))

                def verify_target(candidate, bound_row=row, history=source_history):
                    verified = _verify_connection(
                        candidate, bundle, allow_previous=False,
                        expected_company_id=bound_row["id"],
                        expected_database_id=bound_row["database_id"],
                        expected_taxpayer_id=bound_row["taxpayer_id"],
                    )
                    _require_company_history(candidate, verified, history[0])

                upgrade_database(
                    connection, bundle=bundle, kind="company",
                    verify_source_content=verify_source,
                    verify_target=verify_target, fault=fault,
                )
            results.append({"company_id": row["id"], "status": "upgraded"})
        if catalog_version == bundle.current_versions["catalog"]:
            catalog_status = "verified_skip"
        else:
            with closing(connect(root / "catalog.sqlite", validator=lambda c: verify_schema(
                c, bundle=bundle, kind="catalog", allow_previous=True
            ))) as connection:
                catalog_history = []

                def verify_source(candidate):
                    _, current_rows = _catalog_rows(
                        candidate, root, bundle,
                        allow_previous=True, require_completed=True,
                    )
                    if current_rows != rows:
                        raise KernelError("catalog_content_invalid", "升级前登记公司已变化")
                    retained = _catalog_retained_digest(candidate)
                    if catalog_history:
                        if retained != catalog_history[0]:
                            raise KernelError("migration_integrity_failed", "目录身份或历史已变化")
                    else:
                        catalog_history.append(retained)

                def verify_target(candidate):
                    verify_schema(candidate, bundle=bundle, kind="catalog")
                    _, current_rows = _catalog_rows(
                        candidate, root, bundle,
                        allow_previous=False, require_completed=True,
                    )
                    if current_rows != rows:
                        raise KernelError("catalog_content_invalid", "升级改变了登记公司内容")
                    if _catalog_retained_digest(candidate) != catalog_history[0]:
                        raise KernelError("migration_integrity_failed", "目录身份或历史已变化")

                upgrade_database(
                    connection, bundle=bundle, kind="catalog",
                    verify_source_content=verify_source,
                    verify_target=verify_target, fault=fault,
                )
            catalog_status = "upgraded"
        check_root_current(root, bundle=bundle)
        return {
            "status": "upgraded",
            "companies": results,
            "catalog": catalog_status,
            "database_format": {
                kind: bundle.database_format(kind) for kind in ("company", "catalog")
            },
        }
