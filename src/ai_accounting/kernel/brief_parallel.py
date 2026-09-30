"""Service-owned, bounded parallel reads for one complete dashboard brief.

Workers never share a connection, QueryReads proof, or business result cache.
The parent accepts their values only when a separate autocommit guard has seen
no committed change across every read transaction.
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event, Lock, Thread

from .contracts import KernelError

_WORKER_POOL = None
_WORKER_BUNDLE = None


def _file_identity(path):
    info = Path(path).stat()
    return info.st_dev, info.st_ino, info.st_size


class BriefReadGuard:
    """Observe commits on one connection that never starts a transaction."""

    def __init__(self, store):
        self.store = store
        self.connection = None
        self._connection_context = None
        self.file_identity = None
        self.data_version = None

    def _version(self):
        with closing(self.connection.execute("PRAGMA data_version")) as cursor:
            return cursor.fetchone()[0]

    def __enter__(self):
        context = self.store.connection(read_only=True)
        self.connection = context.__enter__()
        self._connection_context = context
        try:
            if self.connection.in_transaction:
                raise RuntimeError("brief guard must remain autocommit")
            self.file_identity = _file_identity(self.store.path)
            self.data_version = self._version()
            if self.connection.in_transaction:
                raise RuntimeError("brief guard opened a read transaction")
            return self
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def unchanged(self):
        try:
            return (
                self.connection is not None
                and not self.connection.in_transaction
                and self.file_identity == _file_identity(self.store.path)
                and self.data_version == self._version()
                and not self.connection.in_transaction
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return False

    def worker_matches(self, packet):
        return (
            packet.get("identity_before") == self.file_identity
            and packet.get("identity_after") == self.file_identity
        )

    def __exit__(self, exc_type, exc_value, traceback):
        context = self._connection_context
        connection = self.connection
        self._connection_context = None
        self.file_identity = None
        self.data_version = None
        try:
            if context is None:
                return False
            if exc_type is None:
                try:
                    if connection.in_transaction:
                        raise RuntimeError("brief guard must remain autocommit")
                except BaseException as error:
                    context.__exit__(type(error), error, error.__traceback__)
                    raise
            return context.__exit__(exc_type, exc_value, traceback)
        finally:
            self.connection = None


class ParallelBriefUnavailable(RuntimeError):
    """Discard all worker values and run the original single-snapshot read."""


def _init_worker():
    global _WORKER_POOL, _WORKER_BUNDLE
    from .daemon import _prepare_static_runtime
    from .resident_reads import ResidentReadPool

    _WORKER_BUNDLE = _prepare_static_runtime()[0]
    _WORKER_POOL = ResidentReadPool(maximum=1)


def _error_packet(exc):
    if isinstance(exc, KernelError):
        return {
            "ok": False,
            "error": "kernel",
            "code": exc.code,
            "message": str(exc),
            "details": exc.details,
        }
    return {"ok": False, "error": "worker", "message": repr(exc)}


def _worker_read(kind, binding, period, as_of, send_pipe=None):
    from .engine import Engine
    from .query_reads import QueryReads
    from .storage import Store
    from .types import YearMonth

    path, company_id, database_id, taxpayer_id = binding
    issue_pipe, position_pipe = (
        send_pipe if kind == "reports" and send_pipe is not None else (None, None)
    )
    identity_before = _file_identity(path)
    try:
        store = Store(
            path, _WORKER_BUNDLE, company_id, database_id,
            taxpayer_id=taxpayer_id, read_pool=_WORKER_POOL,
        )
        engine = Engine(store)
        if kind == "reports":
            from .dashboard import Dashboard, _position
            from .reports import check_report_readiness

            with Dashboard(engine)._snapshot(period) as snap:
                snap.as_of = as_of
                issues = check_report_readiness(
                    store, snap.connection, YearMonth(period), reads=snap.reads
                )
                issue_pipe.send({"ok": True, "value": issues})
                # The parent already authenticated this exact settlement scope
                # while building open items. Its result is accepted only with
                # the complete guarded group; it is not a QueryReads proof.
                source = position_pipe.recv()
                if (
                    not isinstance(source, dict)
                    or source.get("binding") != binding
                    or source.get("period") != period
                    or not isinstance(source.get("obligations"), list)
                ):
                    raise ValueError("brief position source belongs to another request")
                value = {"position": _position(
                    snap, position_obligations=source["obligations"]
                )}
        else:
            with QueryReads.snapshot(engine) as reads:
                if kind == "materials":
                    from .materials import _CompletenessInspectionCache, read_completeness_summary

                    closed_through = reads.connection.execute(
                        "SELECT max(period) FROM period_close"
                    ).fetchone()[0]
                    value = read_completeness_summary(
                        reads.connection, YearMonth(period).ordinal, store.registry,
                        closed_through=closed_through,
                        _inspection_cache=_CompletenessInspectionCache(reads.connection),
                        _query_reads=reads,
                    )
                elif kind == "duplicates":
                    from .duplicates import DuplicateCandidates
                    from .materials import _CompletenessInspectionCache

                    value = DuplicateCandidates(store).close_readiness(
                        reads.connection, period,
                        _inspection_cache=_CompletenessInspectionCache(reads.connection),
                        _query_reads=reads,
                    )
                else:
                    raise ValueError("unknown brief worker")
        result = {"ok": True, "value": value}
    except Exception as exc:
        result = _error_packet(exc)
        if issue_pipe is not None:
            try:
                issue_pipe.send(result)
            except (BrokenPipeError, OSError):
                pass
    finally:
        if issue_pipe is not None:
            issue_pipe.close()
        if position_pipe is not None:
            position_pipe.close()
    try:
        identity_after = _file_identity(path)
    except OSError:
        identity_after = None
    return {
        **result,
        "identity_before": identity_before,
        "identity_after": identity_after,
    }


class _BriefParallelAttempt:
    def __init__(self, owner, guard):
        self.owner = owner
        self.guard = guard
        self.started = False
        self.finished = False
        self._results = {}
        self._used = set()
        self._pipe = None
        self._send_pipe = None
        self._position_pipe = None
        self._position_send_pipe = None
        self._position_supplied = False
        self._position_sender = None
        self._position_send_done = Event()
        self._position_send_error = None
        self._deadline = None

    def eligible(self, snap):
        month = snap.month
        connection = snap.connection
        previous = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
        if previous is not None and previous >= month:
            return False
        if "financial_reports" not in snap.store.registry.snapshot_readiness:
            return False
        kinds = sorted(
            kind for kind in snap.store.registry.evaluators
            if snap.store.registry.models[kind].lane != "management"
        )
        if kinds:
            earlier = connection.execute(
                "SELECT 1 FROM fact_revision f INDEXED BY fact_period "
                "CROSS JOIN fact_current c CROSS JOIN subject s "
                "WHERE f.period>? AND f.period<? AND c.fact_id=f.id AND s.id=f.subject_id "
                f"AND s.kind IN({','.join('?' for _ in kinds)}) LIMIT 1",
                (previous if previous is not None else -1, month, *kinds),
            ).fetchone()
            if earlier is not None:
                return False
        return True

    def start(self, snap):
        if not self.eligible(snap):
            return False
        store = snap.store
        binding = (str(store.path), store.company_id, store.database_id, store.taxpayer_id)
        self._pipe, self._send_pipe = self.owner.context.Pipe(duplex=False)
        self._position_pipe, self._position_send_pipe = self.owner.context.Pipe(duplex=False)
        self._deadline = time.monotonic() + self.owner.timeout_seconds
        # A later submission can fail after earlier workers have started.
        # Mark the group live before the first submission so close() kills it.
        self.started = True
        try:
            for kind in ("materials", "duplicates", "reports"):
                self._results[kind] = self.owner.pool.apply_async(
                    _worker_read,
                    (kind, binding, snap.period, snap.as_of,
                     (self._send_pipe, self._position_pipe) if kind == "reports" else None),
                )
        except Exception as exc:
            raise ParallelBriefUnavailable("could not start brief workers") from exc
        return True

    def supply_position_obligations(self, snap):
        """Send the parent's already checked settlement rows for this guarded read."""
        from .dashboard import RECLASS
        from .settlement_projection import settlement_position_rows

        if not self.started or self._position_supplied:
            raise ParallelBriefUnavailable("brief position source is unavailable")
        obligations = settlement_position_rows(
            snap.connection, snap.period, RECLASS, reads=snap.reads
        )
        source = {
            "binding": (
                str(snap.store.path), snap.store.company_id,
                snap.store.database_id, snap.store.taxpayer_id,
            ),
            "period": snap.period,
            "obligations": obligations,
        }

        def send_source():
            try:
                # A large position payload and a large report-issue packet can
                # otherwise fill their separate pipes in opposite directions.
                self._position_send_pipe.send(source)
            except (BrokenPipeError, EOFError, OSError, ValueError) as exc:
                self._position_send_error = exc
            finally:
                self._position_send_done.set()

        self._position_supplied = True
        self._position_sender = Thread(target=send_source, daemon=True)
        self._position_sender.start()

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise ParallelBriefUnavailable("brief worker deadline exceeded")
        return remaining

    def _checked_packet(self, packet):
        if not isinstance(packet, dict) or not self.guard.worker_matches(packet):
            raise ParallelBriefUnavailable("brief worker file identity changed")
        if not packet.get("ok"):
            if packet.get("error") == "kernel":
                raise KernelError(packet["code"], packet["message"], **packet["details"])
            raise ParallelBriefUnavailable("brief worker failed")
        return packet["value"]

    def _get(self, kind):
        try:
            packet = self._results[kind].get(timeout=self._remaining())
        except Exception as exc:
            raise ParallelBriefUnavailable("brief worker unavailable") from exc
        return self._checked_packet(packet)

    def material(self):
        self._used.add("materials")
        return self._get("materials")

    def duplicates(self):
        self._used.add("duplicates")
        return self._get("duplicates")

    def report(self):
        self._used.add("report")
        try:
            if not self._pipe.poll(self._remaining()):
                raise ParallelBriefUnavailable("report issues were not delivered")
            packet = self._pipe.recv()
        except (EOFError, OSError) as exc:
            raise ParallelBriefUnavailable("report issue channel failed") from exc
        if not isinstance(packet, dict) or not packet.get("ok"):
            # The completed packet carries the independently checked file identity.
            self._get("reports")
            raise ParallelBriefUnavailable("report worker did not deliver issues")
        return packet["value"]

    def position(self):
        self._used.add("position")
        position = self._get("reports")["position"]
        if not self._position_send_done.wait(self._remaining()) or self._position_send_error:
            raise ParallelBriefUnavailable("brief position source channel failed")
        return position

    def finish(self):
        if not self.started:
            self.finished = True
            return
        if not self._position_supplied or self._used != {
            "materials", "duplicates", "report", "position"
        }:
            raise ParallelBriefUnavailable("brief did not consume every worker proof")
        for kind in ("materials", "duplicates", "reports"):
            self._get(kind)
        if not self.guard.unchanged():
            raise ParallelBriefUnavailable("brief source changed across snapshots")
        self.finished = True

    def close(self):
        if self._pipe is not None:
            self._pipe.close()
        if self._send_pipe is not None:
            self._send_pipe.close()
        if self._position_pipe is not None:
            self._position_pipe.close()
        if self._position_send_pipe is not None:
            self._position_send_pipe.close()
        if self.started and not self.finished:
            self.owner._recycle()
        if self._position_sender is not None:
            self._position_sender.join(timeout=1)


class BriefParallelPool:
    """One in-flight group of three persistent read-only workers per service."""

    def __init__(self, *, timeout_seconds=15.0):
        self.timeout_seconds = timeout_seconds
        self.context = multiprocessing.get_context("spawn")
        self._gate = Lock()
        self._closed = False
        self.pool = self._new_pool()

    def _new_pool(self):
        return self.context.Pool(processes=3, initializer=_init_worker)

    def _recycle(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            pool.terminate()
            pool.join()

    @contextmanager
    def attempt(self, store):
        if not self._gate.acquire(blocking=False):
            yield None
            return
        try:
            if self._closed:
                yield None
                return
            if self.pool is None:
                self.pool = self._new_pool()
            with BriefReadGuard(store) as guard:
                attempt = _BriefParallelAttempt(self, guard)
                try:
                    yield attempt
                finally:
                    attempt.close()
        finally:
            self._gate.release()

    def close(self):
        with self._gate:
            self._closed = True
            self._recycle()
