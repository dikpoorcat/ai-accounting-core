"""Local catalog, single-company units and verified database copies."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pypinyin import Style, lazy_pinyin

from ai_accounting_modular.contracts.public import ContractError

from ._company import company_metadata, create_database, spec_metadata
from ._database import (
    _candidate,
    _connect,
    _database_error,
    _discard_publication,
    _identifier,
    _initialize,
    _text,
    verify_database,
)
from ._schema import (
    CATALOG_FORMAT,
    packaged_schema,
    verify_structure,
)
from ._session import StorageContext
from ._types import (
    CompanyLocation,
    CompanyMetadata,
    CompanySpec,
    DatabaseSnapshot,
    OwnerProfile,
    RepositoryFactory,
)
from ._units import database_unit, open_database_unit, repository_factories


def create_catalog(path: Path | str, owner: OwnerProfile) -> Path:
    path = Path(path).resolve()
    _validate_owner(owner)

    def seed(connection):
        connection.execute(
            "INSERT INTO catalog_meta VALUES(1,?,?,1,?)",
            (CATALOG_FORMAT, str(uuid4()), packaged_schema("catalog")["fingerprint"]),
        )
        connection.execute(
            "INSERT INTO owner_profile VALUES(1,?,?,?,?)",
            (owner.owner_id, owner.display_name, owner.password_hash, owner.recovery_hash),
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


def _validate_owner(owner: OwnerProfile) -> None:
    if not isinstance(owner, OwnerProfile):
        raise ContractError("invalid_input", "须提供明确的老板身份与凭据摘要。")
    _identifier(owner.owner_id, "owner_id")
    _text(owner.display_name, "老板姓名")
    _text(owner.password_hash, "密码摘要", max_length=4096)
    if owner.recovery_hash is not None:
        _text(owner.recovery_hash, "恢复码摘要", max_length=4096)


def _owner_profile(connection: sqlite3.Connection) -> OwnerProfile:
    row = connection.execute("SELECT * FROM owner_profile WHERE singleton=1").fetchone()
    if row is None:
        raise ContractError("storage_corrupt", "目录缺少老板身份")
    profile = OwnerProfile(
        row["owner_id"], row["display_name"], row["password_hash"], row["recovery_hash"]
    )
    _validate_owner(profile)
    return profile


def _catalog_identity(connection: sqlite3.Connection) -> None:
    catalog = connection.execute("SELECT catalog_id FROM catalog_meta WHERE singleton=1").fetchone()
    if catalog is None:
        raise ContractError("storage_corrupt", "目录缺少目录或老板身份")
    try:
        _identifier(catalog["catalog_id"], "catalog_id")
        _owner_profile(connection)
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
        self._factories = repository_factories(repositories)
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

    def catalog_identity(self) -> str:
        with self._catalog(writable=False) as catalog:
            return catalog.execute(
                "SELECT catalog_id FROM catalog_meta WHERE singleton=1"
            ).fetchone()[0]

    def owner_profile(self) -> OwnerProfile:
        with self._catalog(writable=False) as catalog:
            return _owner_profile(catalog)

    def replace_owner_credentials(
        self, expected: OwnerProfile, *, password_hash: str, recovery_hash: str | None
    ) -> OwnerProfile:
        _validate_owner(expected)
        replacement = OwnerProfile(
            expected.owner_id, expected.display_name, password_hash, recovery_hash
        )
        _validate_owner(replacement)
        with self._catalog(writable=True) as catalog:
            if _owner_profile(catalog) != expected:
                raise ContractError("conflict", "老板凭据已变化，请重新登录后再修改。")
            catalog.execute(
                "UPDATE owner_profile SET password_hash=?,recovery_hash=? WHERE singleton=1",
                (password_hash, recovery_hash),
            )
        return replacement

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
        metadata = spec_metadata(spec)
        root, path, backup = (
            metadata.company_root,
            metadata.database_path,
            metadata.backup_directory,
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
                    publication = create_database(metadata)
                except ContractError as exc:
                    if exc.code == "target_exists":
                        raise ContractError(
                            "target_exists",
                            "目标已存在，不能覆盖；若为上次中断留下的公司库，核验后通过显式登记接续",
                        ) from exc
                    raise
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
        return metadata

    def register_company(
        self, company_root: Path | str, *, backup_directory: Path | str | None = None
    ) -> CompanyMetadata:
        """Explicitly register an independently verified new/recovered company location."""
        root = Path(company_root).resolve()
        path = root / "runtime/company.sqlite3"
        snapshot = verify_database(path)
        with open_database_unit(path, company_root=root, backup_directory=backup_directory) as read:
            metadata = company_metadata(read)
            if (
                metadata.database_id != snapshot.database_id
                or metadata.company_id != snapshot.company_id
            ):
                raise ContractError("conflict", "登记前公司库身份已变化，请重新核验")
        with self._catalog(writable=True) as catalog:
            try:
                catalog.execute(
                    "INSERT INTO company_location VALUES(?,?,?,?,?,?,?,'active')",
                    (
                        snapshot.company_id,
                        metadata.credit_code,
                        metadata.legal_name,
                        str(root),
                        str(path),
                        snapshot.database_id,
                        str(metadata.backup_directory),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ContractError("conflict", "公司身份或位置已登记，不能替换现有公司") from exc
        return metadata

    @contextmanager
    def _unit(
        self,
        company_id: str,
        *,
        writable: bool,
        maintenance: bool = False,
        preview: bool = False,
        view: Literal["current", "as_posted"] = "current",
        as_of: str | None = None,
    ):
        location = self._location(company_id)
        with database_unit(
            location,
            self._factories,
            writable=writable,
            maintenance=maintenance,
            preview=preview,
            view=view,
            as_of=as_of,
            expected_identity=(company_id, location["database_id"], location["credit_code"]),
        ) as context:
            yield context

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

    def preview_unit(self, company_id: str):
        """Run the same accounting checks, then discard every database change."""
        return self._unit(company_id, writable=True, preview=True)

    def maintenance_unit(self, company_id: str):
        return self._unit(company_id, writable=True, maintenance=True)

    def company_metadata(self, context: StorageContext) -> CompanyMetadata:
        return company_metadata(context)

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
