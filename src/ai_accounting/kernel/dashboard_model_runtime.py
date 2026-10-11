"""Transaction-owned production of derived owner pages, never a write API."""

from __future__ import annotations

import base64
import copy
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from .contracts import KernelError
from .storage import _active_fact_reads
from .types import canonical, digest

INTERNAL_REQUEST_PREFIX = "dashboard-read-model/"
_producing = ContextVar("dashboard_model_producing", default=False)
_changed_periods = ContextVar("dashboard_model_changed_periods", default=None)
_REFRESH_PLACEHOLDER = "0" * 64


def producing_model():
    return _producing.get()


def supports_native_production(engine):
    """Five-page production is paired with the active package's full bundle.

    Isolated kernel registries and historical content verifiers still validate
    their exact schemas. They cannot acquire today's UI model as a side effect
    of a typed business write.
    """
    from .schema_bundle import production_bundle

    if getattr(engine.store.registry, "content_version", None) == 1:
        return False
    active = production_bundle()
    return (
        engine.store.bundle.database_format() == active.database_format()
        and active.registry.models.keys() <= engine.store.registry.models.keys()
    )


@contextmanager
def changed_periods(connection, *, enabled=True):
    """Collect known periods from real writes, not scans or response bags."""
    if _changed_periods.get() is not None or not connection.in_transaction:
        raise KernelError("dashboard_model_scope_invalid", "看板月份必须来自当前写事务")
    periods = set()
    token = _changed_periods.set((connection, periods, enabled))
    try:
        yield periods
    finally:
        _changed_periods.reset(token)


def record_period(connection, period):
    """Use a validated fact, publication, inventory or close's actual period."""
    from .types import YearMonth

    active = _changed_periods.get()
    if active is None:
        return
    if active[0] is not connection:
        raise KernelError("dashboard_model_scope_invalid", "看板月份不能跨公司写事务复用")
    if not active[2]:
        return
    month = YearMonth.from_ordinal(period) if type(period) is int else YearMonth(period)
    if month.ordinal != 0:
        active[1].add(str(month))


def stable_payload(payload):
    """Separate explicit refresh metadata from the certified business bytes.

    Dates and identities remain real. Only versions and the display generation
    timestamp are regenerated from the authenticated root at the read boundary.
    Page cursors retain their exact seek key and ordering profile.
    """
    from .stored_json import loads_unique

    value = copy.deepcopy(payload)
    if "snapshot_version" in value:
        value["snapshot_version"] = _REFRESH_PLACEHOLDER
    value["read_context"]["read_version"] = _REFRESH_PLACEHOLDER
    if "checked_at" in value:
        value["checked_at"] = "2000-01-01T00:00:00+00:00"
    data = value.get("data")
    if data is not None:
        if value.get("scope") == "overview":
            data["generated_at"] = "2000-01-01T00:00:00+00:00"
        for collection in data.get("collections", {}).values():
            page = collection["page"]
            if page.get("next_cursor") is not None:
                decoded = loads_unique(base64.urlsafe_b64decode(page["next_cursor"].encode()))
                if set(decoded) != {"scope", "key", "sort"}:
                    raise KernelError("content_integrity_failed", "看板内部分页合同不一致")
                page["next_cursor"] = base64.urlsafe_b64encode(canonical({
                    **decoded, "scope": _REFRESH_PLACEHOLDER,
                }).encode()).decode()
    return value


def model_scope(engine, connection, period, *, as_of=None):
    from .business_queries import _today_china
    from .dashboard_read_model import ModelEpochs, ModelScope
    from .engine import PROGRAM_VERSION
    from .read_state import repair_revision

    return ModelScope(
        company_id=engine.store.company_id,
        database_id=engine.store.database_id,
        period=period,
        epochs=ModelEpochs(**engine.store.epochs(connection)),
        read_repair_revision=repair_revision(connection),
        rule_build=PROGRAM_VERSION,
        as_of=as_of or _today_china(),
    )


def supports_model(connection):
    """Capability belongs to the package's checked structural contract."""
    return connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' "
        "AND name='dashboard_read_model_commit'"
    ).fetchone() is not None


def authenticated_model(dashboard, snap):
    """Reuse one complete root proof only inside this owned read snapshot.

    Exact focused reads also compose an overview. Both consumers need the same
    certified directory, not two independent decodes of its growing root.
    Returned source bodies and page blocks still perform their own hit checks.
    """
    from .dashboard_read_model import load_model
    from .query_reads import _owns_current_selector_snapshot

    if producing_model() or snap is None:
        return None
    owned = _owns_current_selector_snapshot(snap.reads, snap.connection)
    key = ("authenticated_dashboard_model", snap.period, snap.as_of)
    if owned and key in snap.semantic_cache:
        return snap.semantic_cache[key]
    if not supports_model(snap.connection):
        return None
    model = load_model(snap.connection, model_scope(
        dashboard.engine, snap.connection, snap.period, as_of=snap.as_of,
    ))
    # Failure and absence are not successful proofs. An unmanaged transaction
    # may consume the same bytes but cannot retain a proof for later calls.
    if model is not None and owned:
        snap.semantic_cache[key] = model
    return model


def _independent_outer_read(reads, connection):
    """A different protected reader may be suspended, never used as a proof."""
    from .query_reads import QueryReads, _owns_current_selector_snapshot

    return reads is None or (
        isinstance(reads, QueryReads)
        and reads.connection is not connection
        and _owns_current_selector_snapshot(reads, reads.connection)
        and reads.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    )


@contextmanager
def production_reads(engine, connection):
    """Read pending writes with guarded snapshot proofs, without a second DB.

    Both query_only and an authorizer prohibit writes, transaction changes and
    disabling the guard. An unrelated owned reader may stay open on another
    connection; its snapshot and proofs are suspended, never borrowed by the
    writer. Nothing this view proves survives its transaction.
    """
    from .query_reads import QueryReads

    if (
        not connection.in_transaction
        or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        or not _independent_outer_read(_active_fact_reads.get(), connection)
        or producing_model()
    ):
        raise KernelError("dashboard_model_scope_invalid", "看板生产必须使用独立写事务")
    allowed = {
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    read_pragmas = {
        "schema_version", "user_version", "application_id", "foreign_keys",
        "database_list", "table_info", "table_xinfo", "index_list", "index_info",
        "index_xinfo", "foreign_key_list", "query_only",
        "function_list",
    }

    def read_only(action, first, second, *_):
        if action in allowed:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and first.lower() in read_pragmas:
            if first.lower() in {"table_info", "table_xinfo", "index_list", "index_info",
                                 "index_xinfo", "foreign_key_list"} or second is None:
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    connection.execute("PRAGMA query_only=ON")
    reads = QueryReads(engine, connection)
    reads._snapshot_active = True
    token = _active_fact_reads.set(reads)
    producer_token = _producing.set(True)
    connection.set_authorizer(read_only)
    original_failure = None
    try:
        yield reads
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        _producing.reset(producer_token)
        _active_fact_reads.reset(token)
        reads._reset()
        try:
            connection.set_authorizer(None)
            connection.execute("PRAGMA query_only=OFF")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 0:
                raise RuntimeError("writer read guard could not be restored")
        except BaseException:
            connection.close()
            if original_failure is None:
                raise
            original_failure.add_note("看板生产读取保护恢复失败，连接已关闭")


def materialize_models(engine, connection, *, periods=(), evidence_registration=False):
    """Produce registered periods after business receipts, before its COMMIT.

    Existing periods register through explicit maintenance; actual typed writes
    register their own periods. Ordinary reads never fill or repair this model.
    Its internal audit is separate from the untouched business write receipt.
    """
    if not supports_native_production(engine) or not supports_model(connection):
        if periods:
            raise KernelError("dashboard_model_unavailable", "当前结构尚不支持看板读取模型")
        return ()
    from .dashboard import Dashboard, _Snapshot
    from .dashboard_model_materializer import build_model
    from .dashboard_read_model import (
        PageBlock,
        authenticate_current_storage,
        collect_obsolete_blocks,
        load_current_model,
        persist_model,
        prepare_evidence_registration_model,
        prepare_model,
        supports_native_content,
    )
    from .read_indexes import sync_audit
    from .types import YearMonth

    registered = {row[0] for row in connection.execute(
        "SELECT DISTINCT period FROM dashboard_read_model_commit"
    )}
    requested = registered | {str(YearMonth(period)) for period in periods}
    if not requested:
        return ()
    # Authenticate the existing current frames before replacement. A normal
    # write cannot mask damaged current content by producing a new frame.
    # Do this once for the whole production batch, not once per period.
    authenticate_current_storage(connection)
    request_id = INTERNAL_REQUEST_PREFIX + uuid.uuid4().hex
    prepared = []
    with production_reads(engine, connection) as reads:
        dashboard = Dashboard(engine)

        @contextmanager
        def snapshot(period):
            snap = _Snapshot(engine, connection, period, reads)
            try:
                yield snap
            finally:
                snap.release()

        dashboard._snapshot = snapshot
        for period in sorted(requested):
            scope = model_scope(engine, connection, period)
            # Authentic staleness is okay after a business write; corruption is
            # never treated as a reason to silently regenerate the old model.
            existing = load_current_model(connection, scope)
            if (existing is not None and existing.descriptor.scope == scope
                and supports_native_content(existing.descriptor)):
                continue
            if evidence_registration and existing is not None:
                rebound = prepare_evidence_registration_model(
                    existing, scope, request_id=request_id,
                )
                if rebound is not None:
                    prepared.append(rebound)
                    continue
            current = connection.execute(
                "SELECT commit_id FROM dashboard_read_model_current WHERE period=?", (period,)
            ).fetchone()
            content = build_model(dashboard, period)
            blocks = [PageBlock(
                block.key, block.kind, stable_payload(block.payload), keys=block.keys,
                sources=block.sources, voucher_version_ids=block.voucher_version_ids,
                total_count=block.total_count, filtered_count=block.filtered_count,
                item_sources=block.item_sources,
                item_voucher_version_ids=block.item_voucher_version_ids,
                fact_sources=block.fact_sources,
                item_fact_sources=block.item_fact_sources,
            ) for block in content.blocks]
            prepared.append(prepare_model(
                scope, blocks, request_id=request_id,
                parent_commit_id=bytes(current[0]).hex() if current else None,
                date_basis=content.date_basis,
            ))
    if not prepared:
        return ()
    engine.fault("dashboard_materialized", connection)
    result = {"status": "materialized", "dashboard_models": [
        item.descriptor for item in prepared
    ]}
    audit_payload = (
        {"result": result, "actor": engine.audit_actor}
        if engine.audit_actor is not None else result
    )
    audit = connection.execute(
        "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
        (request_id, "dashboard_read_model", canonical(audit_payload)),
    )
    sync_audit(connection, audit.lastrowid)
    connection.execute(
        "INSERT INTO request VALUES(?,?,?)",
        (request_id, digest(["dashboard_read_model", result]), canonical(result)),
    )
    for item in prepared:
        persist_model(connection, item, audit_id=audit.lastrowid, collect_obsolete=False)
    # Historical receipts and their parent chains remain immutable; only UI
    # bytes no current root refers to can be reclaimed in this transaction.
    collect_obsolete_blocks(connection)
    engine.fault("dashboard_persisted", connection)
    return tuple(item.descriptor for item in prepared)
