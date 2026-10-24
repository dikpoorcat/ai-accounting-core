"""Database creation, exact validation and consistent physical copies."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from ai_accounting_modular.contracts.public import ContractError, YearMonth, validate_contract

from ._schema import company_schema, install_structure, verify_structure
from ._types import DatabaseSnapshot


def _month(value: str) -> str:
    try:
        return str(YearMonth(value))
    except ValueError as exc:
        raise ContractError("invalid_input", "月份必须是有效的 YYYY-MM") from exc


def _database_error(exc: sqlite3.DatabaseError) -> ContractError:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return ContractError("busy", "公司正在处理另一项工作，请稍后重试")
    return ContractError("storage_corrupt", "数据库文件损坏或无法完成当前操作")


def _identifier(value: str, field: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ContractError("invalid_input", f"{field} 必须是规范 UUID") from exc
    return value


def _text(value: str, field: str, *, max_length: int = 200) -> str:
    if type(value) is not str or not value.strip() or len(value) > max_length:
        raise ContractError("invalid_input", f"{field} 不能为空且不能超过 {max_length} 个字符")
    return value


def _company_identity(
    credit_code, legal_name, start_period, *, accounting_standard="small_enterprise", currency="CNY"
) -> None:
    validate_contract(
        "core.schema.json",
        "CompanyIdentity",
        {
            "credit_code": credit_code,
            "legal_name": legal_name,
            "start_period": start_period,
            "accounting_standard": accounting_standard,
            "currency": currency,
        },
    )


def _stored_company(connection: sqlite3.Connection) -> tuple[sqlite3.Row, sqlite3.Row]:
    system = connection.execute("SELECT * FROM system_meta WHERE singleton=1").fetchone()
    company = connection.execute("SELECT * FROM company_profile WHERE singleton=1").fetchone()
    if system is None or company is None:
        raise ContractError("storage_corrupt", "数据库缺少公司或数据库身份")
    try:
        _identifier(company["company_id"], "company_id")
        _identifier(system["database_id"], "database_id")
        _company_identity(
            company["credit_code"],
            company["legal_name"],
            company["start_period"],
            accounting_standard=company["accounting_standard"],
            currency=company["currency"],
        )
    except ContractError as exc:
        raise ContractError("storage_corrupt", "数据库保存的公司或数据库身份不合法") from exc
    return system, company


def _new_target(path: Path) -> None:
    candidates = (Path(str(path) + suffix) for suffix in ("", "-wal", "-shm", "-journal"))
    if any(candidate.exists() or candidate.is_symlink() for candidate in candidates):
        raise ContractError("target_exists", "目标数据库或旁文件已存在，不能覆盖")


@dataclass(frozen=True)
class _Publication:
    device: int
    inode: int
    sha256: str


@dataclass
class _Candidate:
    path: Path
    publication: _Publication | None = None


def _publication(path: Path) -> _Publication:
    identity = path.stat()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return _Publication(identity.st_dev, identity.st_ino, digest)


def _discard_publication(path: Path, publication: _Publication) -> bool:
    """Undo only this verified new file; changed files and outside sidecars are retained."""
    try:
        if path.is_symlink():
            return False
        if not path.exists():
            return True
        sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
        if any(sidecar.exists() or sidecar.is_symlink() for sidecar in sidecars):
            return False
        if _publication(path) != publication:
            return False
        identity = path.stat()
        if (identity.st_dev, identity.st_ino) != (publication.device, publication.inode):
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _connect(path: Path, *, writable: bool) -> sqlite3.Connection:
    # URI mode prevents SQLite silently creating a missing runtime database.
    uri = path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro")
    try:
        connection = sqlite3.connect(
            uri, uri=True, isolation_level=None, timeout=3, cached_statements=0
        )
    except sqlite3.DatabaseError as exc:
        raise ContractError("storage_corrupt", "数据库文件不存在、损坏或无法打开") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA read_uncommitted=OFF")
        connection.execute("PRAGMA busy_timeout=3000")
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise ContractError("storage_corrupt", "数据库没有采用本体系规定的 WAL 模式")
        if writable:
            connection.execute("PRAGMA synchronous=FULL")
            # KiB, allocated on demand for this connection's write working set.
            connection.execute("PRAGMA cache_size=-32768")
        else:
            connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise _database_error(exc) from exc
    except BaseException:
        connection.close()
        raise


def _publish(source: Path, target: Path) -> None:
    _new_target(target)
    with source.open("r+b") as stream:
        os.fsync(stream.fileno())
    if os.name == "nt":
        os.rename(source, target)
    else:
        # link is exclusive, unlike rename on POSIX.
        os.link(source, target)
        source.unlink()
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@contextmanager
def _candidate(target: Path) -> Iterator[_Candidate]:
    _new_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = None
    try:
        with TemporaryDirectory(prefix=".database-create-", dir=target.parent) as directory:
            candidate = _Candidate(Path(directory) / "database.sqlite3")
            yield candidate
            candidate.publication = _publication(candidate.path)
            _publish(candidate.path, target)
    except BaseException as exc:
        if (
            candidate is not None
            and candidate.publication is not None
            and not _discard_publication(target, candidate.publication)
        ):
            exc.add_note(f"新目标发生变化，已保留供核验：{target}")
        raise


def _initialize(path: Path, kind: str, seed) -> None:
    with closing(sqlite3.connect(path, isolation_level=None, cached_statements=0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            install_structure(connection, kind)
            seed(connection)
            verify_structure(connection, kind)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise ContractError("storage_corrupt", "数据库无法启用 WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def verify_database(path: Path | str, *, structure_version: int | None = None) -> DatabaseSnapshot:
    expected = company_schema(structure_version)
    path = Path(path).resolve()
    connection = _connect(path, writable=False)
    try:
        connection.execute("BEGIN")
        verify_structure(connection, "company", expected=expected)
        if (
            connection.execute("PRAGMA quick_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
        ):
            raise ContractError("storage_corrupt", "数据库完整性检查失败")
        system, company = _stored_company(connection)
        return DatabaseSnapshot(
            path,
            company["company_id"],
            system["database_id"],
            system["global_seq"],
            system["structure_fingerprint"],
        )
    except sqlite3.DatabaseError as exc:
        raise ContractError("storage_corrupt", "数据库文件损坏或无法识别") from exc
    finally:
        connection.rollback()
        connection.close()


def restore_database_copy(
    source: Path | str, target: Path | str, *, structure_version: int | None = None
) -> DatabaseSnapshot:
    source, target = Path(source).resolve(), Path(target).resolve()
    verify_database(source, structure_version=structure_version)
    with _candidate(target) as candidate:
        with (
            closing(_connect(source, writable=False)) as origin,
            closing(sqlite3.connect(candidate.path)) as destination,
        ):
            origin.execute("BEGIN")
            origin.backup(destination)
        snapshot = verify_database(candidate.path, structure_version=structure_version)
    return DatabaseSnapshot(
        target,
        snapshot.company_id,
        snapshot.database_id,
        snapshot.head,
        snapshot.structure_fingerprint,
    )
