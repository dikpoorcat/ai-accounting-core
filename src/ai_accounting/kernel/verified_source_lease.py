"""One-call reuse of independently verified source rows inside one SQLite transaction.

The private savepoint is a transaction witness: COMMIT/ROLLBACK removes it, so a
token cannot be reused after the same connection starts another transaction.
No source result is cached beyond the caller's verification scope.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class VerifiedSourceLease:
    def __init__(self, connection):
        if not connection.in_transaction:
            raise ValueError("source reuse requires an active transaction")
        self.connection = connection
        self.name = "verified_source_" + uuid.uuid4().hex
        self.active = True
        connection.execute("SAVEPOINT " + self.name)

    def require(self, connection):
        if not self.active or self.connection is not connection or not connection.in_transaction:
            raise ValueError("verified source belongs to another verification scope")
        try:
            # RELEASE fails if the original transaction ended, even when the
            # same connection has since started a fresh transaction.
            connection.execute("RELEASE SAVEPOINT " + self.name)
        except sqlite3.OperationalError as exc:
            self.active = False
            raise ValueError("verified source transaction has ended") from exc
        try:
            connection.execute("SAVEPOINT " + self.name)
        except sqlite3.Error:
            self.active = False
            raise

    def close(self):
        if not self.active:
            return
        self.active = False
        if self.connection.in_transaction:
            try:
                self.connection.execute("RELEASE SAVEPOINT " + self.name)
            except sqlite3.OperationalError:
                pass


_current: ContextVar[VerifiedSourceLease | None] = ContextVar("verified_source_lease", default=None)


@contextmanager
def verified_source_lease(connection):
    lease = VerifiedSourceLease(connection)
    token = _current.set(lease)
    try:
        yield lease
    finally:
        _current.reset(token)
        lease.close()


def current_verified_lease(connection) -> VerifiedSourceLease:
    lease = _current.get()
    if lease is None:
        raise ValueError("verified source reuse requires its original verification scope")
    lease.require(connection)
    return lease


def require_verified_lease(connection, lease: VerifiedSourceLease):
    if lease is not _current.get() or not isinstance(lease, VerifiedSourceLease):
        raise ValueError("verified source belongs to another verification scope")
    lease.require(connection)


@dataclass(frozen=True)
class VerifiedCalculationSource:
    connection: object
    lease: VerifiedSourceLease
    source: dict


def verified_calculation_source(connection, source: dict) -> VerifiedCalculationSource:
    return VerifiedCalculationSource(connection, current_verified_lease(connection), source)


def require_verified_calculation_source(connection, token) -> dict:
    if type(token) is not VerifiedCalculationSource or token.connection is not connection:
        raise ValueError("verified calculations belong to another connection")
    require_verified_lease(connection, token.lease)
    return token.source
