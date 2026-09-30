"""A v1 close prefix is usable only in its verified read transaction."""

import sqlite3

import pytest

from ai_accounting.kernel.position_v1 import _require_close_prefix, _verified_close_prefix
from ai_accounting.kernel.verified_source_lease import verified_source_lease


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE period_close(period INTEGER PRIMARY KEY,digest BLOB NOT NULL)")
    connection.executemany(
        "INSERT INTO period_close(period,digest) VALUES (?,?)", [(1, b"first"), (3, b"third")]
    )
    connection.commit()
    connection.execute("BEGIN")
    return connection


def _closes(connection):
    return tuple(
        (row, {"period": row["period"], "vouchers": []})
        for row in connection.execute("SELECT * FROM period_close ORDER BY period")
    )


def test_v1_verified_close_prefix_covers_exact_saved_history_and_transaction():
    connection = _connection()
    other = _connection()
    try:
        closes = _closes(connection)
        with verified_source_lease(connection):
            prefix = _verified_close_prefix(connection, closes)
            assert _require_close_prefix(connection, prefix, 3) == closes
            with pytest.raises(ValueError, match="another connection"):
                _require_close_prefix(other, prefix, 3)
            for invalid in (closes[:1], closes[::-1], (closes[1],)):
                with pytest.raises(ValueError):
                    _require_close_prefix(
                        connection, _verified_close_prefix(connection, invalid), 3
                    )
            changed = (closes[0], ({"period": 3, "digest": b"wrong"}, closes[1][1]))
            with pytest.raises(ValueError, match="omit or alter"):
                _require_close_prefix(
                    connection, _verified_close_prefix(connection, changed), 3
                )
            with pytest.raises(ValueError, match="do not end"):
                _require_close_prefix(connection, prefix, 1)
            connection.rollback()
            connection.execute("BEGIN")
            with pytest.raises(ValueError, match="transaction has ended"):
                _require_close_prefix(connection, prefix, 3)
        with pytest.raises(ValueError, match="verification scope"):
            _require_close_prefix(connection, prefix, 3)
    finally:
        connection.close()
        other.close()
