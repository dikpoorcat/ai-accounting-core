"""Verified result reuse cannot outlive its exact SQLite transaction."""

import sqlite3

import pytest

from ai_accounting.kernel.verified_source_lease import (
    current_verified_lease,
    require_verified_lease,
    verified_source_lease,
)


def test_read_only_nested_lease_keeps_caller_savepoint_and_rejects_new_transaction():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        connection.execute("SAVEPOINT caller")
        with verified_source_lease(connection) as outer:
            assert current_verified_lease(connection) is outer
            with verified_source_lease(connection) as inner:
                assert current_verified_lease(connection) is inner
                require_verified_lease(connection, inner)
            require_verified_lease(connection, outer)
        connection.execute("RELEASE SAVEPOINT caller")
        with pytest.raises(ValueError, match="another verification scope"):
            require_verified_lease(connection, outer)
        connection.commit()

        connection.execute("BEGIN")
        with verified_source_lease(connection) as old:
            connection.rollback()
            connection.execute("BEGIN")
            with pytest.raises(ValueError, match="transaction has ended"):
                require_verified_lease(connection, old)
        with verified_source_lease(connection) as fresh:
            assert current_verified_lease(connection) is fresh
            with pytest.raises(ValueError, match="another verification scope"):
                require_verified_lease(connection, old)
