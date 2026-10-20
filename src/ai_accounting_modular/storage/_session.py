"""Owner repository capabilities bound to one storage-owned transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Literal

from ai_accounting_modular.contracts.public import ContractError

from ._types import RepositoryFactory

MAINTENANCE_TABLES = {
    "records": frozenset({"document_shard_head", "document_set"}),
    "reading": frozenset({"read_scope_head", "read_component", "read_item"}),
    "files": frozenset({"file_task"}),
}
DERIVED_TABLES = frozenset().union(*MAINTENANCE_TABLES.values())
Parameters = Sequence[Any] | Mapping[str, Any]
_MUTATIONS = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
_FORBIDDEN = frozenset(
    {
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_PRAGMA,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_REINDEX,
    }
)


class RepositorySession:
    """Injected only into the owner's private repository, never a business API."""

    def __init__(self, unit: StorageContext, tables: frozenset[str]):
        self.__unit = unit
        self.__tables = tables

    def _run(
        self,
        sql: str,
        parameters: Parameters | Iterable[Parameters],
        *,
        many: bool = False,
        result: Literal["count", "one", "all"] = "count",
    ):
        unit = self.__unit
        unit._require_active()
        mutated: set[str] = set()

        def authorize(action, table, column, database, trigger):
            if action in _FORBIDDEN:
                return sqlite3.SQLITE_DENY
            if action in _MUTATIONS:
                if not unit._writable or table not in self.__tables:
                    return sqlite3.SQLITE_DENY
                mutated.add(table)
            return sqlite3.SQLITE_OK

        connection = unit._connection
        before = connection.total_changes
        connection.set_authorizer(authorize)
        cursor = None
        try:
            cursor = (
                connection.executemany(sql, parameters)
                if many
                else connection.execute(sql, parameters)
            )
            if result == "one":
                return cursor.fetchone()
            if result == "all":
                return cursor.fetchall()
            return cursor.rowcount
        except BaseException as exc:
            unit._failed = True
            if isinstance(exc, sqlite3.DatabaseError) and (
                "not authorized" in str(exc) or "readonly" in str(exc)
            ):
                raise ContractError(
                    "invalid_operation", "仓储不能越过表归属或当前事务的读写边界"
                ) from exc
            raise
        finally:
            if cursor is not None:
                cursor.close()
            # RETURNING statements report changes only after their cursor is consumed/closed.
            if connection.total_changes > before and mutated - DERIVED_TABLES:
                unit._business_changed = True
            connection.set_authorizer(None)

    def execute(self, sql: str, parameters: Parameters = ()) -> int:
        return self._run(sql, parameters)

    def executemany(self, sql: str, parameters: Iterable[Parameters]) -> int:
        return self._run(sql, parameters, many=True)

    def fetchone(self, sql: str, parameters: Parameters = ()) -> sqlite3.Row | None:
        return self._run(sql, parameters, result="one")

    def fetchall(self, sql: str, parameters: Parameters = ()) -> list[sqlite3.Row]:
        return self._run(sql, parameters, result="all")


class StorageContext:
    def __init__(
        self,
        connection: sqlite3.Connection,
        company_id: str,
        database_id: str,
        head: int,
        factories: dict[str, RepositoryFactory],
        *,
        writable: bool,
        maintenance: bool = False,
        view: Literal["current", "as_posted"] = "current",
        as_of: str | None = None,
    ):
        self.company_id = company_id
        self.database_id = database_id
        self.head = head
        self.view = view
        self.as_of = as_of
        self._connection = connection
        self._writable = writable
        self._maintenance = maintenance
        self._factories = factories
        self._repositories: dict[str, Any] = {}
        self._business_changed = False
        self._failed = False
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise ContractError("invalid_operation", "工作单元已结束，不能继续使用仓储")

    def repository(self, owner: str) -> Any:
        self._require_active()
        if owner not in self._repositories:
            factory = self._factories.get(owner)
            if factory is None:
                raise ContractError("invalid_operation", f"模块仓储尚未装配：{owner}")
            if self._maintenance:
                build = factory.maintenance_build
                if build is None or owner not in MAINTENANCE_TABLES:
                    raise ContractError("invalid_operation", f"模块没有维护仓储：{owner}")
                tables = MAINTENANCE_TABLES[owner]
            else:
                build, tables = factory.build, factory.schema.tables
            self._repositories[owner] = build(RepositorySession(self, tables))
        return self._repositories[owner]


class StorageUnitOfWork(StorageContext):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.next_head = self.head if self._maintenance else self.head + 1
        self._checks: list[Callable[[], None]] = []

    def check_before_commit(self, callback: Callable[[], None]) -> None:
        self._require_active()
        self._checks.append(callback)
