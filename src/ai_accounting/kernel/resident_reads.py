"""Bounded, service-owned SQLite read connections; never stores business results."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Condition

from .contracts import KernelError
from .permissions import assert_private_file
from .runtime import (
    RuntimeConfigurationError,
    _install_owned_read_guard,
    _owned_read_transaction_operation,
    _validate_reused_read_connection_at_private_path,
    connect,
    require_local_database,
)

# Keep SQLite's own coherent page cache bounded across the four resident reads.
# Smaller caches repeatedly evict necessary pages at the main scale;
# this stores database pages, never a previous transaction's business response.
READ_PAGE_CACHE_KIB = 256 * 1024
MAXIMUM_READ_CONNECTIONS = 4


@dataclass
class _IdleRead:
    path: Path
    signature: tuple[int, int]
    connection: object
    owned_snapshot: bool = False


class ResidentReadPool:
    """At most four checked-out or idle connections across all companies."""

    def __init__(self, *, maximum=MAXIMUM_READ_CONNECTIONS, wait_seconds=5.0):
        if type(maximum) is not int or not 1 <= maximum <= MAXIMUM_READ_CONNECTIONS:
            raise ValueError("read pool must have one to four connections")
        self.maximum = maximum
        self.wait_seconds = wait_seconds
        self._condition = Condition()
        self._idle: list[_IdleRead] = []
        self._total = 0
        self._closed = False

    @staticmethod
    def _signature(path):
        database = require_local_database(path)
        assert_private_file(database)
        info = database.stat()
        return database, (info.st_dev, info.st_ino)

    def _take(self, path, *, owned_snapshot=False):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            old = None
            with self._condition:
                if self._closed:
                    raise KernelError("service_stopping", "服务正在停止")
                for index, candidate in enumerate(self._idle):
                    if candidate.path == path and candidate.owned_snapshot == owned_snapshot:
                        return self._idle.pop(index), False
                if self._total < self.maximum:
                    self._total += 1
                    return None, True
                if self._idle:
                    old = self._idle.pop(0)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise KernelError("database_busy", "只读连接均在使用，请稍后重试")
                    self._condition.wait(remaining)
            if old is not None:
                try:
                    old.connection.close()
                except BaseException:
                    self._cancel_open()
                    raise
                return None, True

    def _release(self, entry, *, discard):
        if entry.connection is None:
            return
        with self._condition:
            close = discard or self._closed
            if close:
                self._total -= 1
            else:
                self._idle.append(entry)
            self._condition.notify()
        if close:
            connection, entry.connection = entry.connection, None
            connection.close()

    def _cancel_open(self):
        with self._condition:
            self._total -= 1
            self._condition.notify()

    @contextmanager
    def borrow(self, store, *, _owned_snapshot=False):
        path = store.path
        while True:
            entry, create = self._take(path, owned_snapshot=_owned_snapshot)
            if create:
                try:
                    database, before = self._signature(path)
                    connection = connect(
                        database, read_only=True, validator=store.validate_connection,
                        _cross_thread=True,
                        _owned_snapshot=_owned_snapshot,
                    )
                    try:
                        connection.execute(f"PRAGMA cache_size=-{READ_PAGE_CACHE_KIB}")
                        if _owned_snapshot:
                            _install_owned_read_guard(connection)
                    except BaseException:
                        connection.close()
                        raise
                    entry = _IdleRead(database, before, connection, _owned_snapshot)
                except BaseException:
                    self._cancel_open()
                    raise
            try:
                database, signature = self._signature(path)
                if entry.signature != signature or entry.path != database:
                    self._release(entry, discard=True)
                    continue
                if not create:
                    _validate_reused_read_connection_at_private_path(
                        database, entry.connection, store.validate_connection
                    )
                cache_size = entry.connection.execute("PRAGMA cache_size").fetchone()
                if cache_size is None or cache_size[0] != -READ_PAGE_CACHE_KIB:
                    raise RuntimeConfigurationError("Read connection changed PRAGMA cache_size")
                if self._signature(path)[1] != signature:
                    self._release(entry, discard=True)
                    continue
            except BaseException:
                # The mismatched-file path above has already released its entry.
                if entry.connection is not None:
                    self._release(entry, discard=True)
                raise
            break
        discard = False
        try:
            yield entry.connection
        except BaseException:
            discard = True
            raise
        finally:
            try:
                if not entry.owned_snapshot:
                    entry.connection.set_authorizer(None)
                entry.connection.set_trace_callback(None)
                entry.connection.set_progress_handler(None, 0)
                if entry.connection.in_transaction:
                    if entry.owned_snapshot:
                        # A failed snapshot must never return a connection to idle.
                        discard = True
                        _owned_read_transaction_operation(entry.connection, "ROLLBACK")
                    else:
                        entry.connection.rollback()
                if entry.connection.in_transaction:
                    discard = True
            except BaseException:
                discard = True
                raise
            finally:
                self._release(entry, discard=discard)

    def close(self):
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._total -= len(idle)
            self._condition.notify_all()
        for entry in idle:
            entry.connection.close()
