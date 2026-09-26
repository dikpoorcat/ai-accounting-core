"""Versioned directory and durable company-file publication."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .contracts import KernelError
from .permissions import (
    create_private_file,
    ensure_private_directory,
    private_temporary_directory,
    reject_reparse_path,
)
from .runtime import (
    connect,
    initialize_file,
    private_file_lock,
    publish_database,
    require_local_database,
)
from .schema_bundle import production_bundle
from .storage import Store
from .types import canonical
from .versions import (
    HISTORY_DDL,
    META_DDL,
    check_released_contract,
    database_format,
    execute_statements,
    install_metadata,
    verify_schema,
)

VERSION = 0


@contextmanager
def _archive_snapshot(archive, parent):
    """Hash and parse the same private bytes, never successive opens of an external path."""
    with private_temporary_directory(parent, prefix=".restore-source-") as directory:
        snapshot = create_private_file(directory / "source.zip")
        hashed, size = hashlib.sha256(), 0
        with open(archive, "rb") as source, snapshot.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                hashed.update(chunk)
                size += len(chunk)
                output.write(chunk)
        yield snapshot, hashed.hexdigest(), size


def catalog_sql():
    from .security.schema import CATALOG_DDL

    return (
        """
CREATE TABLE company(id TEXT PRIMARY KEY, taxpayer_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 path TEXT NOT NULL UNIQUE,database_id TEXT NOT NULL UNIQUE) STRICT;
CREATE TABLE catalog_identity(id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL UNIQUE) STRICT;
CREATE TRIGGER immutable_catalog_identity_update BEFORE UPDATE ON catalog_identity
 BEGIN SELECT RAISE(ABORT,'immutable directory identity'); END;
CREATE TRIGGER immutable_catalog_identity_delete BEFORE DELETE ON catalog_identity
 BEGIN SELECT RAISE(ABORT,'immutable directory identity'); END;
CREATE TABLE company_operation(id TEXT PRIMARY KEY, taxpayer_id TEXT NOT NULL UNIQUE,
 kind TEXT NOT NULL CHECK(kind IN('create','restore')), payload TEXT NOT NULL CHECK(json_valid(payload)),
 status TEXT NOT NULL CHECK(status IN('pending','succeeded','failed')),
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT) STRICT;
CREATE TRIGGER immutable_company_operation BEFORE UPDATE OF id,taxpayer_id,kind,payload ON company_operation
 BEGIN SELECT RAISE(ABORT,'immutable company operation'); END;
CREATE TRIGGER retained_company_operation BEFORE DELETE ON company_operation
 BEGIN SELECT RAISE(ABORT,'retained company operation'); END;
CREATE TABLE company_setting(company_id TEXT NOT NULL REFERENCES company(id),
 revision INTEGER NOT NULL CHECK(revision>0),backup_directory TEXT NOT NULL,
 PRIMARY KEY(company_id,revision)) STRICT;
CREATE TRIGGER immutable_company_setting_update BEFORE UPDATE ON company_setting
 BEGIN SELECT RAISE(ABORT,'immutable company setting'); END;
CREATE TRIGGER immutable_company_setting_delete BEFORE DELETE ON company_setting
 BEGIN SELECT RAISE(ABORT,'immutable company setting'); END;
"""  # noqa: E501 -- persisted DDL must retain its exact structural fingerprint
        + HISTORY_DDL
        + META_DDL
        + ";\n".join(CATALOG_DDL)
        + ";\n"
    )


def ensure_catalog(root, bundle=None):
    """Recognize or exclusively create a catalog before any resident-service files."""
    bundle = bundle if bundle is not None else production_bundle()
    root = reject_reparse_path(root)
    path = require_local_database(root / "catalog.sqlite")

    def validate(connection):
        return verify_schema(connection, bundle=bundle, kind="catalog")

    if path.exists():
        with closing(connect(path, read_only=True, validator=validate)):
            pass
        ensure_private_directory(root)
        return path
    reserved = [
        root / name for name in (".operations", ".service.json", ".resident.lock", "service.log")
    ]
    reserved.extend(Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal"))
    if root.exists():
        # Known company locations only; unrelated documents are not a database inventory.
        reserved.extend(root.glob("*/company.sqlite*"))
        reserved.extend(root.glob("companies/*.sqlite*"))
        reserved.extend(root.glob("company.sqlite*"))
    if any(reject_reparse_path(candidate).exists() for candidate in reserved):
        raise KernelError(
            "database_root_unrecognized", "目录库缺失但存在公司或运行文件，拒绝初始化"
        )
    ensure_private_directory(root, parents=True)

    def initialize(connection):
        try:
            connection.execute("BEGIN IMMEDIATE")
            script = catalog_sql()
            check_released_contract(script, bundle=bundle, kind="catalog")
            execute_statements(connection, script)
            connection.execute("INSERT INTO catalog_identity VALUES(1,?)", (uuid.uuid4().hex,))
            install_metadata(connection, bundle=bundle, kind="catalog")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    try:
        initialize_file(path, initialize, validate)
    except FileExistsError:
        # Concurrent creators may only accept the fully published winner.
        with closing(connect(path, read_only=True, validator=validate)):
            pass
    return path


class Catalog:
    def __init__(self, root, bundle=None, *, fault=None):
        self.root = reject_reparse_path(root)
        self.bundle = bundle if bundle is not None else production_bundle()
        self.registry, self.fault = self.bundle.registry, fault
        self.path = ensure_catalog(self.root, self.bundle)
        self.recover_operations()

    def database_format(self):
        with self.connection(read_only=True) as connection:
            return database_format(connection, bundle=self.bundle, kind="catalog")

    @contextmanager
    def connection(self, *, read_only=False):
        with closing(
            connect(
                self.path,
                read_only=read_only,
                validator=lambda c: verify_schema(c, bundle=self.bundle, kind="catalog"),
            )
        ) as connection:
            try:
                yield connection
            finally:
                if connection.in_transaction:
                    connection.rollback()

    def _check(self, point):
        if self.fault:
            self.fault(point)

    def _schedule(self, kind, taxpayer_id, name, *, archive=None):
        from .backup import verify_portable

        if not isinstance(taxpayer_id, str) or not re.fullmatch(r"[0-9A-Z]{18}", taxpayer_id):
            raise ValueError("company requires an 18-character taxpayer identity")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("company requires a name")
        source_hash = None
        source_size = None
        verified = None
        if archive:
            archive = str(reject_reparse_path(archive))
            with _archive_snapshot(archive, self.root) as (snapshot, source_hash, source_size):
                verified = verify_portable(
                    snapshot, expected_taxpayer_id=taxpayer_id, _bundle=self.bundle
                )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            company = connection.execute(
                "SELECT * FROM company WHERE taxpayer_id=?", (taxpayer_id,)
            ).fetchone()
            prior = connection.execute(
                "SELECT * FROM company_operation WHERE taxpayer_id=?", (taxpayer_id,)
            ).fetchone()
            if prior:
                payload = json.loads(prior["payload"])
                if (prior["kind"], payload["name"], payload.get("source_hash")) != (
                    kind,
                    name,
                    source_hash,
                ):
                    raise KernelError(
                        "company_operation_conflict", "该公司已存在不同的创建或恢复操作"
                    )
                operation_id = prior["id"]
                connection.rollback()
                if prior["status"] == "succeeded":
                    return self._operation_result(dict(company), payload)
            elif company:
                if kind != "create" or company["name"] != name:
                    raise KernelError("company_exists", "目标目录已登记该公司")
                return dict(company)
            else:
                identity = (
                    verified["identity"]
                    if verified
                    else {
                        "company_id": uuid.uuid4().hex,
                        "database_id": uuid.uuid4().hex,
                    }
                )
                if connection.execute(
                    "SELECT 1 FROM company WHERE id=? OR database_id=?",
                    (identity["company_id"], identity["database_id"]),
                ).fetchone():
                    raise KernelError("company_exists", "目标目录已登记该数据库身份")
                destination = self.root / taxpayer_id / "company.sqlite"
                if destination.exists():
                    raise KernelError(
                        "unregistered_company_file", "公司路径存在未登记文件，拒绝覆盖"
                    )
                operation_id = uuid.uuid4().hex
                payload = {
                    "id": identity["company_id"],
                    "database_id": identity["database_id"],
                    "taxpayer_id": taxpayer_id,
                    "name": name,
                    "path": str(destination),
                    "archive": archive,
                    "source_hash": source_hash,
                    "source_size": source_size,
                }
                if verified:
                    payload["verification"] = verified["verification"]
                connection.execute(
                    "INSERT INTO company_operation(id,taxpayer_id,kind,payload,status) "
                    "VALUES(?,?,?,?,'pending')",
                    (operation_id, taxpayer_id, kind, canonical(payload)),
                )
                connection.commit()
        self._check("operation_recorded")
        return self._resume(operation_id)

    def _operation_result(self, row, payload):
        if not payload.get("archive"):
            return row
        from .backup import verify_file

        result = verify_file(
            row["path"],
            _bundle=self.bundle,
            expected_company_id=row["id"],
            expected_database_id=row["database_id"],
            expected_taxpayer_id=row["taxpayer_id"],
        )
        return {
            **row,
            "verification": result["verification"],
            "database_format": result["database_format"],
        }

    def _resume(self, operation_id):
        from .backup import restore_portable

        operations = ensure_private_directory(self.root / ".operations")
        with private_file_lock(operations / f"{operation_id}.lock") as acquired:
            if not acquired:
                raise KernelError("company_operation_busy", "公司创建或恢复正在执行，请稍后重试")
            with self.connection(read_only=True) as connection:
                operation = connection.execute(
                    "SELECT * FROM company_operation WHERE id=?", (operation_id,)
                ).fetchone()
                if operation is None:
                    raise KernelError("unknown_company_operation", "公司操作不存在")
                payload = json.loads(operation["payload"])
                row = {
                    key: payload[key]
                    for key in ("id", "taxpayer_id", "name", "path", "database_id")
                }
                if operation["status"] == "succeeded":
                    return self._operation_result(row, payload)
            destination = Path(payload["path"])
            staging = operations / f"{operation_id}.sqlite"
            if destination != self.root / row["taxpayer_id"] / "company.sqlite":
                raise KernelError("company_path_mismatch", "公司操作路径与资料根目录不一致")
            try:
                if not destination.exists():
                    if not staging.exists():
                        if operation["kind"] == "create":
                            Store.create(
                                staging,
                                self.bundle,
                                row["id"],
                                row["taxpayer_id"],
                                row["database_id"],
                            )
                        else:
                            # Freeze the exact scheduled bytes before the backup parser
                            # opens anything. Hashing a mutable path before and after
                            # restore can leave an untrusted staging file for a retry.
                            with _archive_snapshot(payload["archive"], operations) as (
                                snapshot,
                                source_hash,
                                source_size,
                            ):
                                if (
                                    source_hash != payload["source_hash"]
                                    or source_size != payload["source_size"]
                                ):
                                    raise KernelError("restore_source_changed", "恢复资料已改变")
                                restore_portable(
                                    snapshot,
                                    staging,
                                    expected_taxpayer_id=row["taxpayer_id"],
                                    expected_company_id=row["id"],
                                    expected_database_id=row["database_id"],
                                    _bundle=self.bundle,
                                )
                    self._check("file_prepared")
                    candidate = Store(staging, self.bundle, row["id"], row["database_id"])
                    with candidate.connection() as connection:
                        taxpayer_id = connection.execute(
                            "SELECT taxpayer_id FROM identity WHERE id=1"
                        ).fetchone()[0]
                        if taxpayer_id != row["taxpayer_id"]:
                            raise KernelError("company_mismatch", "暂存数据库身份不一致")
                        if tuple(
                            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                        ) != (0, 0, 0):
                            raise KernelError("database_busy", "暂存数据库尚未完成落盘")
                    ensure_private_directory(destination.parent, parents=True)
                    publish_database(staging, destination)
                self._check("file_published")
                self._bound_store(row)
                result = self._operation_result(row, payload)
                # Slow archive and company verification never hold the catalog write lock.
                with self.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("INSERT INTO company VALUES(?,?,?,?,?)", tuple(row.values()))
                    connection.execute(
                        "UPDATE company_operation SET status='succeeded',attempts=attempts+1,"
                        "last_error=NULL WHERE id=?",
                        (operation_id,),
                    )
                    self._check("before_registration_commit")
                    connection.commit()
                self._check("registration_committed")
                return result
            except BaseException as exc:
                if isinstance(exc, Exception):
                    with self.connection() as connection:
                        connection.execute(
                            "UPDATE company_operation SET status='failed',attempts=attempts+1,"
                            "last_error=? WHERE id=? AND status!='succeeded'",
                            (getattr(exc, "code", type(exc).__name__), operation_id),
                        )
                raise

    def recover_operations(self):
        from .diagnostics import operation_error

        with self.connection(read_only=True) as connection:
            pending = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM company_operation WHERE status!='succeeded' "
                    "AND attempts<3 ORDER BY rowid"
                )
            ]
        results = []
        for operation_id in pending:
            try:
                results.append(self._resume(operation_id))
            except Exception as exc:
                code, message = operation_error(getattr(exc, "code", type(exc).__name__))
                results.append(
                    {
                        "operation_id": operation_id,
                        "status": "failed",
                        "code": code,
                        "message": message,
                    }
                )
        return results

    def operations(self):
        from .diagnostics import operation_error

        with self.connection(read_only=True) as connection:
            rows = connection.execute(
                "SELECT id,taxpayer_id,kind,status,attempts,last_error "
                "FROM company_operation ORDER BY rowid"
            )
            result = []
            for row in rows:
                code, message = operation_error(row["last_error"])
                result.append({
                    "id": row["id"],
                    "taxpayer_id": row["taxpayer_id"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "error_code": code if row["status"] == "failed" else None,
                    "error_message": message if row["status"] == "failed" else None,
                })
            return result

    def company_settings(self, company_id: str):
        with self.connection(read_only=True) as connection:
            row = connection.execute(
                "SELECT revision,backup_directory FROM company_setting WHERE company_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (company_id,),
            ).fetchone()
            return dict(row) if row else {"revision": 0, "backup_directory": None}

    def configure_backup(self, company_id: str, backup_directory: str, *, expected_revision: int):
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not backup_directory.strip()
        ):
            raise ValueError("invalid backup setting")
        target = reject_reparse_path(Path(backup_directory).expanduser())
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision = connection.execute(
                "SELECT coalesce(max(revision),0) FROM company_setting WHERE company_id=?",
                (company_id,),
            ).fetchone()[0]
            if revision != expected_revision:
                raise KernelError("setting_version_conflict", "公司备份设置已变化")
            connection.execute(
                "INSERT INTO company_setting VALUES(?,?,?)", (company_id, revision + 1, str(target))
            )
            connection.commit()
            return {
                "status": "configured",
                "revision": revision + 1,
                "backup_directory": str(target),
            }

    def create_company(self, taxpayer_id: str, name: str):
        return self._schedule("create", taxpayer_id, name)

    def restore_company(self, archive: str, *, taxpayer_id: str, name: str):
        return self._schedule("restore", taxpayer_id, name, archive=archive)

    def companies(self):
        with self.connection(read_only=True) as connection:
            return [
                dict(row) for row in connection.execute("SELECT * FROM company ORDER BY name,id")
            ]

    def bind(self, company_id: str):
        with self.connection(read_only=True) as connection:
            row = connection.execute("SELECT * FROM company WHERE id=?", (company_id,)).fetchone()
            if not row:
                raise KernelError("unknown_company", "公司尚未登记")
        return self._bound_store(row)

    def _bound_store(self, row):
        """Bind only a current, fully recognized company; opening never upgrades it."""
        store = Store(row["path"], self.bundle, row["id"], row["database_id"])
        if not store.path.is_file():
            raise KernelError("company_missing", "公司数据库不存在")
        with store.connection(read_only=True) as connection:
            identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
            if identity["taxpayer_id"] != row["taxpayer_id"]:
                raise KernelError("company_mismatch", "数据库身份与目录登记不一致")
        return store
