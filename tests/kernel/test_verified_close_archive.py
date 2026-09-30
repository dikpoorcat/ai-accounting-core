"""Compressed close reuse is tied to the completed verification scope."""

import sqlite3

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.verified_close_archive import (
    VerifiedCloseArchive,
    pack_verified_close,
)
from ai_accounting.kernel.verified_source_lease import verified_source_lease


def test_verified_close_archive_checks_scope_rows_and_nested_lease():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE close_row(period INTEGER PRIMARY KEY,digest BLOB NOT NULL)")
    connection.execute("INSERT INTO close_row VALUES(1,?)", (b"a" * 32,))
    connection.commit()
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    row = connection.execute("SELECT * FROM close_row").fetchone()
    packed = pack_verified_close({"period": "2026-01", "facts": [1, 2]})
    with verified_source_lease(connection):
        archive = VerifiedCloseArchive(connection, [(row, packed)])
        looked_up = archive.lookup(connection, [row])
        assert looked_up[1][1]["facts"] == [1, 2]
        assert archive.lookup(connection, [row])["1"][1]["facts"] == [1, 2]
        with pytest.raises(KernelError, match="关账集合"):
            archive.lookup(connection, [dict(period=1, digest=b"b" * 32)])
        with verified_source_lease(connection):
            with pytest.raises(KernelError, match="当前事务"):
                archive.lookup(connection, [row])
            with pytest.raises(KernelError, match="当前事务"):
                looked_up[1]
        assert archive.lookup(connection, [row])[1][1]["facts"] == [1, 2]
    connection.commit()
    connection.execute("BEGIN")
    with verified_source_lease(connection):
        with pytest.raises(KernelError, match="当前事务"):
            archive.lookup(connection, [row])
        with pytest.raises(KernelError, match="当前事务"):
            looked_up[1]
    connection.rollback()
    connection.close()
