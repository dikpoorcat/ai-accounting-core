"""The single connection factory for company SQLite files.

The runtime is intentionally independent of the existing PostgreSQL application.
Company schemas declare every table STRICT; SQLite has no global STRICT pragma.
The supported interpreter is installed by ``scripts/kernel-runtime.ps1``.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .permissions import (
    PrivatePathError,
    assert_private_file,
    create_private_file,
    ensure_private_file,
    ensure_sqlite_sidecars,
    reject_reparse_path,
)

MIN_SQLITE_VERSION = (3, 51, 3)
RUNTIME_PYTHON_VERSION = "3.12.13"


class RuntimeConfigurationError(RuntimeError):
    """The SQLite library or its connection cannot enforce the kernel contract."""


class _PrivateConnection(sqlite3.Connection):
    """Secure SQLite-created sidecars as soon as the first write creates them."""

    _private_database: Path | None = None
    _private_known_sidecars: set[Path]
    _private_cleanup_sidecars: dict[Path, tuple[int, int]]
    _private_permissions_pending: bool = False

    def _secure_new_sidecars(self) -> None:
        if not self._private_permissions_pending or self._private_database is None:
            return
        database = self._private_database
        current = {
            candidate
            for suffix in ("-wal", "-shm", "-journal")
            if (candidate := Path(str(database) + suffix)).exists()
        }
        if not current:
            return
        ensure_sqlite_sidecars(database, existing=self._private_known_sidecars)
        self._private_known_sidecars = current
        # WAL mode keeps both files for the lifetime of this connection. Any
        # later connection takes a fresh path snapshot before SQLite opens it.
        self._private_permissions_pending = not {
            Path(str(database) + "-wal"),
            Path(str(database) + "-shm"),
        }.issubset(current)

    def execute(self, *args, **kwargs):
        result = super().execute(*args, **kwargs)
        self._secure_new_sidecars()
        return result

    def executemany(self, *args, **kwargs):
        result = super().executemany(*args, **kwargs)
        self._secure_new_sidecars()
        return result

    def executescript(self, *args, **kwargs):
        result = super().executescript(*args, **kwargs)
        self._secure_new_sidecars()
        return result

    def close(self):
        cleanup = getattr(self, "_private_cleanup_sidecars", {})
        self._private_cleanup_sidecars = {}
        super().close()
        _cleanup_empty_wal_pair(cleanup)


def _existing_sidecars(database: Path) -> set[Path]:
    candidates = {Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")}
    for candidate in candidates:
        reject_reparse_path(candidate)
    existing = set()
    # SQLite must not read or recover through a sidecar before its boundary is
    # established. Tightening first still refuses a foreign owner.
    for candidate in candidates:
        for attempt in range(5):
            if not candidate.exists():
                break
            try:
                try:
                    assert_private_file(candidate)
                except PrivatePathError:
                    ensure_private_file(candidate)
                existing.add(candidate)
                break
            except PrivatePathError:
                if not candidate.exists():
                    break
                if attempt == 4:
                    raise
                time.sleep(0.002)
    return existing


def _prepare_sqlite_sidecars(
    database: Path, existing: set[Path]
) -> tuple[set[Path], dict[Path, tuple[int, int]]]:
    prepared = set(existing)
    created = {}
    for suffix in ("-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        if candidate not in prepared:
            try:
                create_private_file(candidate)
                info = candidate.stat()
                created[candidate] = (info.st_dev, info.st_ino)
            except FileExistsError:
                ensure_private_file(candidate)
            prepared.add(candidate)
    return prepared, created


def _database_uses_wal(database: Path) -> bool:
    try:
        with database.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return len(header) == 20 and header[18:20] == b"\x02\x02"


def _cleanup_empty_wal_pair(created: dict[Path, tuple[int, int]]) -> None:
    wal = next((path for path in created if str(path).endswith("-wal")), None)
    shm = next((path for path in created if str(path).endswith("-shm")), None)
    if wal is None or shm is None or len(created) != 2:
        return
    try:
        current = {sidecar: sidecar.stat() for sidecar in created}
        if any(
            (info.st_dev, info.st_ino) != created[sidecar] or info.st_size != 0
            for sidecar, info in current.items()
        ):
            return
        for sidecar in created:
            ensure_private_file(sidecar)
            sidecar.unlink()
    except (FileNotFoundError, PermissionError):
        pass


def require_local_database(path: str | Path):
    """Reject known unsafe active locations; portable packages may live elsewhere."""
    database = reject_reparse_path(path)
    if str(database).startswith(("\\\\", "//")):
        raise RuntimeConfigurationError(
            "Active databases require a local disk, not a network share"
        )
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(str(database.anchor)) == 4:
            raise RuntimeConfigurationError("Active databases cannot use mapped network drives")
        for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            directory = os.environ.get(variable)
            if directory and database.is_relative_to(Path(directory).resolve()):
                raise RuntimeConfigurationError(
                    "Use cloud synchronization for backup packages only"
                )
    return database


def require_supported_runtime() -> None:
    """Reject SQLite releases preceding the WAL-reset corruption fix.

    Source: https://www.sqlite.org/wal.html#walreset
    We deliberately require the mainline fix rather than accepting a collection
    of old releases with individually backported patches.
    """
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise RuntimeConfigurationError(
            "The accounting kernel requires SQLite >= 3.51.3; "
            f"this interpreter provides {sqlite3.sqlite_version}. "
            "Run scripts/kernel-runtime.ps1 and use "
            ".tmp-kernel-venv/Scripts/python.exe."
        )


def connect(
    path: str | Path,
    *,
    read_only: bool = False,
    timeout_seconds: float = 5.0,
    validator=None,
    _permissions: bool = True,
) -> sqlite3.Connection:
    """Open one company file with explicit, durable transactions.

    Callers use ``BEGIN`` for short snapshot reads and ``BEGIN IMMEDIATE`` for
    commits. They must roll back after *any* failure, including a failed COMMIT:
    deferred foreign-key failure leaves a SQLite transaction open. The parent
    directory must already exist. Read-only opens never create a database.
    """
    require_supported_runtime()
    if str(path) == ":memory:":
        raise ValueError("Company databases must be files so WAL durability is available")
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be nonnegative")

    database = require_local_database(path)
    if validator is not None and _permissions:
        # Establish that an existing path is one of our databases before
        # changing its permissions. The actual connection validates again.
        with closing(
            connect(
                database,
                read_only=True,
                timeout_seconds=timeout_seconds,
                _permissions=False,
            )
        ) as probe:
            validator(probe)
    if _permissions:
        if read_only:
            ensure_private_file(database)
        else:
            ensure_private_file(database, create=True)
    existing_sidecars = _existing_sidecars(database)
    created_sidecars: dict[Path, tuple[int, int]] = {}
    if not read_only or existing_sidecars or _database_uses_wal(database):
        existing_sidecars, created_sidecars = _prepare_sqlite_sidecars(
            database, existing_sidecars
        )
    mode = "ro" if read_only else "rw" if validator is not None else "rwc"
    connection = None
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode={mode}",
            uri=True,
            isolation_level=None,
            timeout=timeout_seconds,
            factory=_PrivateConnection,
        )
        connection._private_database = database
        connection._private_known_sidecars = existing_sidecars
        connection._private_cleanup_sidecars = created_sidecars if not _permissions else {}
        connection._private_permissions_pending = _permissions and not existing_sidecars
        connection.row_factory = sqlite3.Row
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA read_uncommitted = OFF")
        connection.execute("PRAGMA synchronous = FULL")
        if validator is not None:
            validator(connection)
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        else:
            mode_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode_row is None or mode_row[0] != "wal":
                raise RuntimeConfigurationError("The company database cannot enable WAL")

        expected = {"foreign_keys": 1, "recursive_triggers": 1, "synchronous": 2}
        expected["read_uncommitted"] = 0
        if read_only:
            expected["query_only"] = 1
        for name, value in expected.items():
            actual = connection.execute(f"PRAGMA {name}").fetchone()
            if actual is None or actual[0] != value:
                raise RuntimeConfigurationError(f"Required SQLite setting unavailable: {name}")
        if _permissions:
            ensure_sqlite_sidecars(database, existing=existing_sidecars)
            connection._private_known_sidecars = {
                candidate
                for suffix in ("-wal", "-shm", "-journal")
                if (candidate := Path(str(database) + suffix)).exists()
            }
            connection._private_permissions_pending = not {
                Path(str(database) + "-wal"),
                Path(str(database) + "-shm"),
            }.issubset(connection._private_known_sidecars)
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        _cleanup_empty_wal_pair(created_sidecars)
        raise
