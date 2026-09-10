"""The single connection factory for company SQLite files.

The runtime is intentionally independent of the existing PostgreSQL application.
Company schemas declare every table STRICT; SQLite has no global STRICT pragma.
The supported interpreter is installed by ``scripts/kernel-runtime.ps1``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

MIN_SQLITE_VERSION = (3, 51, 3)
RUNTIME_PYTHON_VERSION = "3.12.13"


class RuntimeConfigurationError(RuntimeError):
    """The SQLite library or its connection cannot enforce the kernel contract."""


def require_local_database(path: str | Path):
    """Reject known unsafe active locations; portable packages may live elsewhere."""
    database = Path(path).resolve()
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

    database = Path(path).resolve()
    mode = "ro" if read_only else "rwc"
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode={mode}",
        uri=True,
        isolation_level=None,
        timeout=timeout_seconds,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        connection.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.execute("PRAGMA read_uncommitted = OFF")
        connection.execute("PRAGMA synchronous = FULL")
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
        return connection
    except BaseException:
        connection.close()
        raise
