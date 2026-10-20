"""The same company transaction runner serves catalog and explicit-file access."""

import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from ai_accounting_modular.contracts.public import ContractError

from ._database import _connect, _database_error, _month, _stored_company
from ._schema import module_schema, verify_structure
from ._session import StorageContext, StorageUnitOfWork
from ._types import RepositoryFactory

_active_writes: ContextVar[frozenset[Path]] = ContextVar(
    "modular_storage_writes", default=frozenset()
)


def repository_factories(repositories: Iterable[RepositoryFactory]) -> dict[str, RepositoryFactory]:
    factories = {}
    for factory in repositories:
        owner = factory.schema.owner
        if owner in factories or factory.schema != module_schema(owner):
            raise ContractError("invalid_input", "模块仓储重复或结构合同不匹配")
        factories[owner] = factory
    return factories


@contextmanager
def database_unit(
    location,
    factories,
    *,
    writable,
    maintenance=False,
    preview=False,
    view="current",
    as_of=None,
    expected_identity=None,
):
    if (
        view not in {"current", "as_posted"}
        or (view == "current" and as_of is not None)
        or (view == "as_posted" and as_of is None)
    ):
        raise ContractError("invalid_input", "读取口径和截止月份不匹配")
    if as_of is not None:
        _month(as_of)
    path = Path(location["database_path"]).resolve()
    if writable and path in _active_writes.get():
        raise ContractError("invalid_operation", "不能为同一家公司嵌套开启写事务")
    connection = _connect(path, writable=writable)
    context = token = None
    try:
        connection.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
        if writable:
            token = _active_writes.set(_active_writes.get() | {path})
        verify_structure(connection, "company")
        system, profile = _stored_company(connection)
        actual_identity = (profile["company_id"], system["database_id"], profile["credit_code"])
        if expected_identity is not None and actual_identity != expected_identity:
            raise ContractError("storage_corrupt", "公司库身份与目录位置不一致")
        context_type = StorageUnitOfWork if writable else StorageContext
        context = context_type(
            connection,
            profile["company_id"],
            system["database_id"],
            system["global_seq"],
            factories,
            start_period=profile["start_period"],
            writable=writable,
            maintenance=maintenance,
            view=view,
            as_of=as_of,
        )
        context._location = location
        yield context
        if writable:
            if context._failed:
                raise ContractError("invalid_operation", "本工作单元已有失败操作，不能提交部分结果")
            for check in context._checks:
                check()
            if context._failed:
                raise ContractError("invalid_operation", "提交检查中的失败操作不能被忽略")
            if preview:
                connection.rollback()
            else:
                if context._business_changed:
                    connection.execute(
                        "UPDATE system_meta SET global_seq=? WHERE singleton=1",
                        (context.next_head,),
                    )
                connection.commit()
        else:
            connection.rollback()
    except sqlite3.IntegrityError:
        connection.rollback()
        raise
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise _database_error(exc) from exc
    except BaseException:
        connection.rollback()
        raise
    finally:
        if context is not None:
            context._active = False
        if token is not None:
            _active_writes.reset(token)
        connection.close()


def open_database_unit(
    database_path: Path | str,
    *,
    repositories: Iterable[RepositoryFactory] = (),
    company_root: Path | str,
    backup_directory: Path | str | None = None,
    mode: Literal["read", "write", "maintenance"] = "read",
    view: Literal["current", "as_posted"] = "current",
    as_of: str | None = None,
):
    """Select an existing company database without a catalog or implicit creation.

    The caller selects the company material root independently from a captured
    database's physical location. Writable access retains the normal owner
    repository capabilities; maintenance exposes only existing derived owners.
    """
    if mode not in {"read", "write", "maintenance"}:
        raise ContractError("invalid_input", "离线工作单元必须明确读取、业务写入或派生维护模式")
    root = Path(company_root).resolve()
    location = {
        "company_root": root,
        "database_path": Path(database_path).resolve(),
        "backup_directory": Path(backup_directory).resolve()
        if backup_directory is not None
        else root / "backups",
    }
    return database_unit(
        location,
        repository_factories(repositories),
        writable=mode != "read",
        maintenance=mode == "maintenance",
        view=view,
        as_of=as_of,
    )
