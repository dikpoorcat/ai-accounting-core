"""Bounded, service-owned SQLite read connections; never stores business results."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Condition

from .contracts import KernelError
from .permissions import assert_private_file
from .runtime import connect, require_local_database, validate_reused_read_connection

# Keep SQLite's own coherent page cache bounded across the four resident reads.
# The default 2 MiB repeatedly evicts the accounting indexes at the main scale;
# this stores database pages, never a previous transaction's business response.
READ_PAGE_CACHE_KIB = 64 * 1024


@dataclass
class _IdleRead:
    path: Path
    signature: tuple[int, int]
    connection: object


class ResidentReadPool:
    """At most four checked-out or idle connections across all companies."""

    def __init__(self, *, maximum=4, wait_seconds=5.0):
        if maximum < 1:
            raise ValueError("read pool must have at least one connection")
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

    def _take(self, path):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            old = None
            with self._condition:
                if self._closed:
                    raise KernelError("service_stopping", "服务正在停止")
                for index, candidate in enumerate(self._idle):
                    if candidate.path == path:
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
    def borrow(self, store):
        path = store.path
        while True:
            entry, create = self._take(path)
            if create:
                try:
                    database, before = self._signature(path)
                    connection = connect(
                        database, read_only=True, validator=store.validate_connection,
                        _cross_thread=True,
                    )
                    try:
                        connection.execute(f"PRAGMA cache_size=-{READ_PAGE_CACHE_KIB}")
                    except BaseException:
                        connection.close()
                        raise
                    entry = _IdleRead(database, before, connection)
                except BaseException:
                    self._cancel_open()
                    raise
            try:
                database, signature = self._signature(path)
                if entry.signature != signature or entry.path != database:
                    self._release(entry, discard=True)
                    continue
                if not create:
                    validate_reused_read_connection(
                        database, entry.connection, store.validate_connection
                    )
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
                entry.connection.set_authorizer(None)
                entry.connection.set_trace_callback(None)
                entry.connection.set_progress_handler(None, 0)
                if entry.connection.in_transaction:
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
