"""Local catalog, single-company units and verified database copies."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pypinyin import Style, lazy_pinyin

from ai_accounting_modular.contracts.public import ContractError

from ._database import (
    _candidate,
    _company_identity,
    _connect,
    _database_error,
    _discard_publication,
    _identifier,
    _initialize,
    _month,
    _stored_company,
    _text,
    verify_database,
)
from ._schema import (
    CATALOG_FORMAT,
    COMPANY_FORMAT,
    module_schema,
    packaged_schema,
    verify_structure,
)
from ._session import StorageContext, StorageUnitOfWork
from ._types import (
    CompanyLocation,
    CompanyMetadata,
    CompanySpec,
    DatabaseSnapshot,
    OwnerProfile,
    RepositoryFactory,
)

_active_writes: ContextVar[frozenset[tuple[Path, str]]] = ContextVar(
    "modular_storage_writes", default=frozenset()
)


def create_catalog(path: Path | str, owner: OwnerProfile) -> Path:
    path = Path(path).resolve()
    _identifier(owner.owner_id, "owner_id")
    _text(owner.display_name, "老板姓名")
    _text(owner.password_hash, "密码摘要", max_length=4096)

    def seed(connection):
        connection.execute(
            "INSERT INTO catalog_meta VALUES(1,?,?,1,?)",
            (CATALOG_FORMAT, str(uuid4()), packaged_schema("catalog")["fingerprint"]),
        )
        connection.execute(
            "INSERT INTO owner_profile VALUES(1,?,?,?)",
            (owner.owner_id, owner.display_name, owner.password_hash),
        )

    with _candidate(path) as candidate:
        _initialize(candidate.path, "catalog", seed)
    return path


def _location_view(row: sqlite3.Row) -> CompanyLocation:
    try:
        _identifier(row["company_id"], "company_id")
        _identifier(row["database_id"], "database_id")
        if not 1 <= len(row["legal_name"]) <= 200:
            raise ValueError("legal_name")
        if re.fullmatch(r"[0-9A-Z]{18}", row["credit_code"]) is None:
            raise ValueError("credit_code")
        root, database, backup = (
            Path(row["company_root"]),
            Path(row["database_path"]),
            Path(row["backup_directory"]),
        )
        if (
            not root.is_absolute()
            or not backup.is_absolute()
            or database != root / "runtime/company.sqlite3"
        ):
            raise ValueError("company_location")
    except (ContractError, ValueError) as exc:
        raise ContractError("storage_corrupt", "目录中保存的公司身份或位置不合法") from exc
    return CompanyLocation(
        row["company_id"],
        row["database_id"],
        row["credit_code"],
        row["legal_name"],
        root,
        database,
        backup,
    )


def _catalog_identity(connection: sqlite3.Connection) -> None:
    catalog = connection.execute("SELECT catalog_id FROM catalog_meta WHERE singleton=1").fetchone()
    owner = connection.execute("SELECT * FROM owner_profile WHERE singleton=1").fetchone()
    if catalog is None or owner is None:
        raise ContractError("storage_corrupt", "目录缺少目录或老板身份")
    try:
        _identifier(catalog["catalog_id"], "catalog_id")
        _identifier(owner["owner_id"], "owner_id")
        _text(owner["display_name"], "老板姓名")
        _text(owner["password_hash"], "密码摘要", max_length=4096)
    except ContractError as exc:
        raise ContractError("storage_corrupt", "目录保存的目录或老板身份不合法") from exc


def _company_order(location: CompanyLocation) -> tuple[str, str, str]:
    return (
        "".join(lazy_pinyin(location.legal_name, style=Style.NORMAL)).casefold(),
        location.legal_name,
        location.company_id,
    )


class Storage:
    def __init__(self, catalog_path: Path | str, *, repositories: Iterable[RepositoryFactory] = ()):
        self.catalog_path = Path(catalog_path).resolve()
        self._factories: dict[str, RepositoryFactory] = {}
        for factory in repositories:
            owner = factory.schema.owner
            if owner in self._factories or factory.schema != module_schema(owner):
                raise ContractError("invalid_input", "模块仓储重复或结构合同不匹配")
            self._factories[owner] = factory
        with self._catalog(writable=False):
            pass

    @contextmanager
    def _catalog(self, *, writable: bool):
        connection = _connect(self.catalog_path, writable=writable)
        try:
            connection.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            verify_structure(connection, "catalog")
            _catalog_identity(connection)
            yield connection
            if writable:
                connection.commit()
            else:
                connection.rollback()
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise _database_error(exc) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_companies(self) -> tuple[CompanyLocation, ...]:
        with self._catalog(writable=False) as catalog:
            locations = catalog.execute(
                "SELECT * FROM company_location WHERE state='active'"
            ).fetchall()
        return tuple(sorted((_location_view(row) for row in locations), key=_company_order))

    def _location(self, company_id: str) -> sqlite3.Row:
        _identifier(company_id, "company_id")
        with self._catalog(writable=False) as connection:
            row = connection.execute(
                "SELECT * FROM company_location WHERE company_id=? AND state='active'",
                (company_id,),
            ).fetchone()
        if row is None:
            raise ContractError("not_found", "目录中没有这家公司")
        _location_view(row)
        return row

    def create_company(self, spec: CompanySpec) -> CompanyMetadata:
        _identifier(spec.company_id, "company_id")
        _identifier(spec.database_id, "database_id")
        _company_identity(spec.credit_code, spec.legal_name, spec.start_period)
        root = Path(spec.company_root).resolve()
        path = root / "runtime/company.sqlite3"
        backup = (
            Path(spec.backup_directory).resolve()
            if spec.backup_directory is not None
            else root / "backups"
        )

        def seed(connection):
            connection.execute(
                "INSERT INTO system_meta VALUES(1,?,?,1,?,0)",
                (COMPANY_FORMAT, spec.database_id, packaged_schema("company")["fingerprint"]),
            )
            connection.execute(
                "INSERT INTO company_profile VALUES(1,?,?,?,'small_enterprise','CNY',?,1)",
                (spec.company_id, spec.credit_code, spec.legal_name, str(spec.start_period)),
            )
            for owner in sorted({item["owner"] for item in packaged_schema("company")["objects"]}):
                contract = module_schema(owner)
                connection.execute(
                    "INSERT INTO module_schema VALUES(?,?,?,?,?)",
                    (owner, 1, contract.contract_digest, None, contract.sql_fingerprint),
                )

        publication = None
        try:
            # Catalog and filesystem cannot commit atomically. Compensate ordinary failures;
            # a process interruption leaves a verified file for explicit register_company.
            with self._catalog(writable=True) as catalog:
                exists = catalog.execute(
                    "SELECT 1 FROM company_location WHERE company_id=? OR credit_code=? "
                    "OR company_root=? OR database_path=? OR database_id=?",
                    (spec.company_id, spec.credit_code, str(root), str(path), spec.database_id),
                ).fetchone()
                if exists:
                    raise ContractError("conflict", "公司身份或位置已登记")
                try:
                    with _candidate(path) as candidate:
                        _initialize(candidate.path, "company", seed)
                except ContractError as exc:
                    if exc.code == "target_exists":
                        raise ContractError(
                            "target_exists",
                            "目标已存在，不能覆盖；若为上次中断留下的公司库，核验后通过显式登记接续",
                        ) from exc
                    raise
                publication = candidate.publication
                catalog.execute(
                    "INSERT INTO company_location VALUES(?,?,?,?,?,?,?,'active')",
                    (
                        spec.company_id,
                        spec.credit_code,
                        spec.legal_name,
                        str(root),
                        str(path),
                        spec.database_id,
                        str(backup),
                    ),
                )
        except BaseException as exc:
            if publication is not None:
                try:
                    with self._catalog(writable=False) as catalog:
                        registered = (
                            catalog.execute(
                                "SELECT 1 FROM company_location "
                                "WHERE database_path=? OR database_id=?",
                                (str(path), spec.database_id),
                            ).fetchone()
                            is not None
                        )
                except BaseException:
                    raise ContractError(
                        "creation_incomplete",
                        "无法确认目录登记结果，公司库已保留，请核验实际状态后接续",
                    ) from exc
                if not registered and not _discard_publication(path, publication):
                    raise ContractError(
                        "creation_incomplete",
                        "目录登记失败，目标文件发生变化，已保留供核验后显式接续",
                    ) from exc
            raise
        return CompanyMetadata(
            spec.company_id,
            spec.database_id,
            spec.credit_code,
            spec.legal_name,
            str(spec.start_period),
            root,
            path,
            backup,
        )

    def register_company(
        self, company_root: Path | str, *, backup_directory: Path | str | None = None
    ) -> CompanyMetadata:
        """Explicitly register an independently verified new/recovered company location."""
        root = Path(company_root).resolve()
        path = root / "runtime/company.sqlite3"
        snapshot = verify_database(path)
        connection = _connect(path, writable=False)
        try:
            connection.execute("BEGIN")
            system, row = _stored_company(connection)
            if (
                system["database_id"] != snapshot.database_id
                or row["company_id"] != snapshot.company_id
            ):
                raise ContractError("conflict", "登记前公司库身份已变化，请重新核验")
        finally:
            connection.close()
        backup = (
            Path(backup_directory).resolve() if backup_directory is not None else root / "backups"
        )
        with self._catalog(writable=True) as catalog:
            try:
                catalog.execute(
                    "INSERT INTO company_location VALUES(?,?,?,?,?,?,?,'active')",
                    (
                        snapshot.company_id,
                        row["credit_code"],
                        row["legal_name"],
                        str(root),
                        str(path),
                        snapshot.database_id,
                        str(backup),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ContractError("conflict", "公司身份或位置已登记，不能替换现有公司") from exc
        return CompanyMetadata(
            snapshot.company_id,
            snapshot.database_id,
            row["credit_code"],
            row["legal_name"],
            row["start_period"],
            root,
            path,
            backup,
            row["profile_seq"],
        )

    @contextmanager
    def _unit(
        self,
        company_id: str,
        *,
        writable: bool,
        maintenance: bool = False,
        view: Literal["current", "as_posted"] = "current",
        as_of: str | None = None,
    ):
        if (
            view not in {"current", "as_posted"}
            or (view == "current" and as_of is not None)
            or (view == "as_posted" and as_of is None)
        ):
            raise ContractError("invalid_input", "读取口径和截止月份不匹配")
        if as_of is not None:
            _month(as_of)
        write_key = (self.catalog_path, company_id)
        if writable and write_key in _active_writes.get():
            raise ContractError("invalid_operation", "不能为同一家公司嵌套开启写事务")
        location = self._location(company_id)
        connection = _connect(Path(location["database_path"]), writable=writable)
        context = None
        token = None
        try:
            connection.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            if writable:
                token = _active_writes.set(_active_writes.get() | {write_key})
            verify_structure(connection, "company")
            system, profile = _stored_company(connection)
            if (
                profile["company_id"] != company_id
                or system["database_id"] != location["database_id"]
                or profile["credit_code"] != location["credit_code"]
            ):
                raise ContractError("storage_corrupt", "公司库身份与目录位置不一致")
            context_type = StorageUnitOfWork if writable else StorageContext
            context = context_type(
                connection,
                company_id,
                system["database_id"],
                system["global_seq"],
                self._factories,
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
                    raise ContractError(
                        "invalid_operation", "本工作单元已有失败操作，不能提交部分结果"
                    )
                for check in context._checks:
                    check()
                if context._failed:
                    raise ContractError("invalid_operation", "提交检查中的失败操作不能被忽略")
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

    def read_unit(
        self,
        company_id: str,
        *,
        view: Literal["current", "as_posted"] = "current",
        as_of: str | None = None,
    ):
        return self._unit(company_id, writable=False, view=view, as_of=as_of)

    def write_unit(self, company_id: str):
        return self._unit(company_id, writable=True)

    def maintenance_unit(self, company_id: str):
        return self._unit(company_id, writable=True, maintenance=True)

    def company_metadata(self, context: StorageContext) -> CompanyMetadata:
        context._require_active()
        row = context._connection.execute(
            "SELECT company_id,credit_code,legal_name,start_period,profile_seq "
            "FROM company_profile WHERE singleton=1"
        ).fetchone()
        location = context._location
        return CompanyMetadata(
            row["company_id"],
            context.database_id,
            row["credit_code"],
            row["legal_name"],
            row["start_period"],
            Path(location["company_root"]),
            Path(location["database_path"]),
            Path(location["backup_directory"]),
            row["profile_seq"],
        )

    def capture_database(self, context: StorageContext, target: Path | str) -> DatabaseSnapshot:
        context._require_active()
        if context._writable:
            raise ContractError("invalid_operation", "数据库快照必须使用已建立的只读一致点")
        target = Path(target).resolve()
        with _candidate(target) as candidate:
            destination = sqlite3.connect(candidate.path)
            try:
                context._connection.backup(destination)
            finally:
                destination.close()
            snapshot = verify_database(candidate.path)
            if (
                snapshot.company_id != context.company_id
                or snapshot.database_id != context.database_id
                or snapshot.head != context.head
            ):
                raise ContractError("storage_corrupt", "数据库快照没有保持所选一致点")
        return DatabaseSnapshot(
            target,
            snapshot.company_id,
            snapshot.database_id,
            snapshot.head,
            snapshot.structure_fingerprint,
        )
