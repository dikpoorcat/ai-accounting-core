"""Local company directory. No accounting transaction spans company databases."""

from __future__ import annotations

import re
import uuid
from contextlib import closing
from pathlib import Path

from .contracts import KernelError
from .runtime import connect, require_local_database
from .storage import Store


class Catalog:
    def __init__(self, root, registry):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite"
        require_local_database(self.path)
        self.registry = registry
        with closing(connect(self.path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS company(id TEXT PRIMARY "
                "KEY, taxpayer_id TEXT NOT NULL UNIQUE, "
                "name TEXT NOT NULL,path TEXT NOT NULL "
                "UNIQUE,database_id TEXT NOT NULL UNIQUE) STRICT"
            )

    def create_company(self, taxpayer_id: str, name: str):
        if not re.fullmatch(r"[0-9A-Z]{18}", taxpayer_id) or not name.strip():
            raise ValueError("company requires an 18-character taxpayer identity and a name")
        with closing(connect(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                old = connection.execute(
                    "SELECT * FROM company WHERE taxpayer_id=?", (taxpayer_id,)
                ).fetchone()
                if old:
                    if old["name"] != name:
                        raise KernelError("company_exists", "同一公司身份已登记不同名称")
                    connection.rollback()
                    return dict(old)
                cid, database_id = uuid.uuid4().hex, uuid.uuid4().hex
                path = self.root / taxpayer_id / "company.sqlite"
                store = Store.create(path, self.registry, cid, taxpayer_id, database_id)
                connection.execute(
                    "INSERT INTO company VALUES(?,?,?,?,?)",
                    (cid, taxpayer_id, name, str(store.path), database_id),
                )
                connection.commit()
                return {
                    "id": cid,
                    "taxpayer_id": taxpayer_id,
                    "name": name,
                    "path": str(store.path),
                    "database_id": database_id,
                }
            except BaseException:
                connection.rollback()
                raise

    def companies(self):
        with closing(connect(self.path, read_only=True)) as connection:
            return [
                dict(row) for row in connection.execute("SELECT * FROM company ORDER BY name,id")
            ]

    def restore_company(self, archive: str, *, taxpayer_id: str, name: str):
        """Import one verified portable company into an absent directory entry."""
        from .backup import restore_portable, verify_portable

        if not re.fullmatch(r"[0-9A-Z]{18}", taxpayer_id) or not name.strip():
            raise ValueError("company requires an 18-character taxpayer identity and a name")
        verified = verify_portable(archive, expected_taxpayer_id=taxpayer_id)
        identity = verified["identity"]
        with closing(connect(self.path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM company WHERE taxpayer_id=? OR id=? OR database_id=?",
                    (taxpayer_id, identity["company_id"], identity["database_id"]),
                ).fetchone():
                    raise KernelError("company_exists", "目标目录已登记该公司或数据库身份")
                destination = self.root / taxpayer_id / "company.sqlite"
                restore_portable(
                    archive,
                    destination,
                    expected_taxpayer_id=taxpayer_id,
                    expected_company_id=identity["company_id"],
                    expected_database_id=identity["database_id"],
                )
                row = {
                    "id": identity["company_id"],
                    "taxpayer_id": taxpayer_id,
                    "name": name,
                    "path": str(destination),
                    "database_id": identity["database_id"],
                }
                connection.execute("INSERT INTO company VALUES(?,?,?,?,?)", tuple(row.values()))
                connection.commit()
                return row
            except BaseException:
                connection.rollback()
                raise

    def bind(self, company_id: str):
        with closing(connect(self.path, read_only=True)) as connection:
            row = connection.execute("SELECT * FROM company WHERE id=?", (company_id,)).fetchone()
            if not row:
                raise KernelError("unknown_company", "公司尚未登记")
        store = Store(row["path"], self.registry, company_id, row["database_id"])
        with store.connection(read_only=True):
            pass
        return store
