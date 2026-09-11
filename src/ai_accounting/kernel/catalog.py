"""Versioned directory and durable company-file publication."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .contracts import KernelError
from .runtime import connect, require_local_database
from .storage import Store
from .types import canonical
from .versions import (
    HISTORY_DDL,
    baseline,
    check_released_contract,
    current_version,
    execute_statements,
    record_version,
    upgrade,
    verify_schema,
)

VERSION = 3


def catalog_sql():
    from .security.schema import CATALOG_DDL

    return (
        baseline("catalog")["objects"][0]["sql"]
        + ";"
        + """
CREATE TABLE catalog_identity(id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL UNIQUE,
 schema_version INTEGER NOT NULL) STRICT;
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
        + ";\n".join(CATALOG_DDL)
        + ";\n"
    )


class Catalog:
    def __init__(self, root, registry, *, fault=None):
        self.root = Path(root).resolve()
        self.path = require_local_database(self.root / "catalog.sqlite")
        self.registry, self.fault = registry, fault
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("xb"):
                pass
            fresh = True
        except FileExistsError:
            fresh = False
        validator = (
            None if fresh else lambda c: verify_schema(c, kind="catalog", allow_previous=True)
        )
        with closing(connect(self.path, validator=validator)) as connection:
            if fresh:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    script = catalog_sql()
                    check_released_contract(script, kind="catalog")
                    execute_statements(connection, script)
                    connection.execute(
                        "INSERT INTO catalog_identity VALUES(1,?,?)", (uuid.uuid4().hex, VERSION)
                    )
                    connection.execute(f"PRAGMA user_version={VERSION}")
                    record_version(connection, VERSION)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            else:
                upgrade(connection, kind="catalog")
            verify_schema(connection, kind="catalog")
        self.recover_operations()

    @contextmanager
    def connection(self, *, read_only=False):
        with closing(
            connect(
                self.path, read_only=read_only, validator=lambda c: verify_schema(c, kind="catalog")
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
        if archive:
            archive = str(Path(archive).resolve())
            with open(archive, "rb") as source:
                source_hash = hashlib.file_digest(source, "sha256").hexdigest()
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
                    return dict(company)
            elif company:
                if kind != "create" or company["name"] != name:
                    raise KernelError("company_exists", "目标目录已登记该公司")
                return dict(company)
            else:
                identity = (
                    verify_portable(archive, expected_taxpayer_id=taxpayer_id)["identity"]
                    if archive
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
                }
                connection.execute(
                    "INSERT INTO company_operation(id,taxpayer_id,kind,payload,status) "
                    "VALUES(?,?,?,?,'pending')",
                    (operation_id, taxpayer_id, kind, canonical(payload)),
                )
                connection.commit()
        self._check("operation_recorded")
        return self._resume(operation_id)

    def _resume(self, operation_id):
        from .backup import _publish_new, restore_portable

        # The catalog lock serializes interrupted operation recovery, independently
        # of accounting transactions in each company's own database.
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT * FROM company_operation WHERE id=?", (operation_id,)
            ).fetchone()
            payload = json.loads(operation["payload"])
            row = {
                key: payload[key] for key in ("id", "taxpayer_id", "name", "path", "database_id")
            }
            if operation["status"] == "succeeded":
                return row
            destination = Path(payload["path"])
            staging = self.root / ".operations" / f"{operation_id}.sqlite"
            if destination != self.root / row["taxpayer_id"] / "company.sqlite":
                raise KernelError("company_path_mismatch", "公司操作路径与资料根目录不一致")
            try:
                if not destination.exists():
                    staging.parent.mkdir(exist_ok=True)
                    if staging.exists():
                        with closing(connect(staging, read_only=True)) as candidate:
                            empty = (
                                candidate.execute(
                                    "SELECT 1 FROM sqlite_schema WHERE sql IS NOT NULL LIMIT 1"
                                ).fetchone()
                                is None
                            )
                        if empty:
                            # An interrupted exclusive create can leave a zero-schema file.
                            # Only this operation's exact private staging file is disposable.
                            for owned in (
                                staging,
                                Path(str(staging) + "-wal"),
                                Path(str(staging) + "-shm"),
                            ):
                                if owned.resolve().parent != (self.root / ".operations").resolve():
                                    raise KernelError("company_path_mismatch", "暂存路径不一致")
                                owned.unlink(missing_ok=True)
                    if not staging.exists():
                        if operation["kind"] == "create":
                            Store.create(
                                staging,
                                self.registry,
                                row["id"],
                                row["taxpayer_id"],
                                row["database_id"],
                            )
                        else:
                            with open(payload["archive"], "rb") as source:
                                if (
                                    hashlib.file_digest(source, "sha256").hexdigest()
                                    != payload["source_hash"]
                                ):
                                    raise KernelError("restore_source_changed", "恢复资料已改变")
                            restore_portable(
                                payload["archive"],
                                staging,
                                expected_taxpayer_id=row["taxpayer_id"],
                                expected_company_id=row["id"],
                                expected_database_id=row["database_id"],
                            )
                    self._check("file_prepared")

                    def validate_staging(candidate):
                        verify_schema(candidate, registry=self.registry, allow_previous=True)
                        identity = candidate.execute("SELECT * FROM identity WHERE id=1").fetchone()
                        if (
                            identity["company_id"],
                            identity["database_id"],
                            identity["taxpayer_id"],
                        ) != (row["id"], row["database_id"], row["taxpayer_id"]):
                            raise KernelError("company_mismatch", "暂存数据库身份不一致")

                    with closing(connect(staging, validator=validate_staging)) as candidate:
                        upgrade(candidate, registry=self.registry)
                    with Store(staging, self.registry, row["id"], row["database_id"]).connection():
                        pass
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _publish_new(staging, destination)
                self._check("file_published")
                self._bound_store(row)
                connection.execute("INSERT INTO company VALUES(?,?,?,?,?)", tuple(row.values()))
                connection.execute(
                    "UPDATE company_operation SET status='succeeded',attempts=attempts+1,"
                    "last_error=NULL WHERE id=?",
                    (operation_id,),
                )
                self._check("before_registration_commit")
                connection.commit()
                self._check("registration_committed")
                return row
            except BaseException as exc:
                connection.rollback()
                if isinstance(exc, Exception):
                    connection.execute(
                        "UPDATE company_operation SET status='failed',attempts=attempts+1,"
                        "last_error=? WHERE id=? AND status!='succeeded'",
                        (getattr(exc, "code", type(exc).__name__), operation_id),
                    )
                raise

    def recover_operations(self):
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
                results.append(
                    {
                        "operation_id": operation_id,
                        "status": "failed",
                        "code": getattr(exc, "code", type(exc).__name__),
                    }
                )
        return results

    def operations(self):
        with self.connection(read_only=True) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT id,taxpayer_id,kind,status,attempts,last_error "
                    "FROM company_operation ORDER BY rowid"
                )
            ]

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
        target = Path(backup_directory).expanduser().resolve()
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
        """Upgrade only an exactly known, identity-bound company; current reads stay read-only."""
        store = Store(row["path"], self.registry, row["id"], row["database_id"])
        if not store.path.is_file():
            raise KernelError("company_missing", "company database is missing")

        def validate(connection):
            version = verify_schema(connection, registry=self.registry, allow_previous=True)
            identity = connection.execute("SELECT * FROM identity WHERE id=1").fetchone()
            if (identity["company_id"], identity["database_id"], identity["taxpayer_id"]) != (
                row["id"],
                row["database_id"],
                row["taxpayer_id"],
            ):
                raise KernelError("company_mismatch", "数据库身份与目录登记不一致")
            return version

        with closing(connect(store.path, read_only=True)) as connection:
            version = validate(connection)
        if version < current_version("business"):
            # connect validates the read probe and the actual write handle before
            # enabling WAL. upgrade rechecks the contract under BEGIN IMMEDIATE.
            with closing(connect(store.path, validator=validate)) as connection:
                upgrade(connection, registry=self.registry, fault=self._check)
        return store
