"""Explicit, package-owned migration operations. No external SQL execution API."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from .contracts import KernelError

ConnectionAction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationStep:
    kind: str
    source_version: int
    source_sha256: str
    target_version: int
    target_sha256: str
    apply: ConnectionAction
    validate_data: ConnectionAction
    requires_fk_off: bool = False

    def __post_init__(self):
        if self.kind not in {"business", "catalog"} or not (
            0 <= self.source_version < self.target_version
        ):
            raise ValueError("invalid migration step versions")
        for digest in (self.source_sha256, self.target_sha256):
            if len(digest) != 64 or bytes.fromhex(digest).hex() != digest:
                raise ValueError("invalid migration step fingerprint")


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class TableReplacement:
    """Declared new-table/copy/drop/rename recipe, including every attached object.

    create_sql names the temporary replacement table. copy_sql explicitly lists
    destination columns and source expressions. SQLite's exact post-rename SQL
    must be the SQL captured by the target contract (including its quoting).
    """

    table: str
    replacement: str
    create_sql: str
    copy_sql: str
    restore_objects: tuple[tuple[str, str, str], ...]

    def apply(self, connection, *, fault=None):
        if not connection.in_transaction or connection.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise KernelError("migration_integrity_failed", "表复制需要已关闭外键的升级事务")
        attached = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type,name FROM sqlite_schema WHERE tbl_name=? "
                "AND type IN ('index','trigger') AND sql IS NOT NULL",
                (self.table,),
            )
        }
        declared = {(typ, name) for typ, name, _ in self.restore_objects}
        if attached != declared or len(declared) != len(self.restore_objects):
            raise KernelError("migration_not_declared", "表复制关联对象未完整声明")
        connection.execute(self.create_sql)
        if fault:
            fault("after_create")
        connection.execute(self.copy_sql)
        if fault:
            fault("after_copy")
        connection.execute(f"DROP TABLE {_identifier(self.table)}")
        if fault:
            fault("after_drop")
        connection.execute(
            f"ALTER TABLE {_identifier(self.replacement)} RENAME TO {_identifier(self.table)}"
        )
        if fault:
            fault("after_rename")
        for _, _, sql in self.restore_objects:
            connection.execute(sql)
        if fault:
            fault("after_restore")
