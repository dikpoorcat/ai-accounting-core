"""Durable coalesced owner projections, built outside authoritative writes.

Selector statements come exclusively from the trusted native producer. Their
ordered result digests include empty hits, old scopes and cumulative ranges.
No API accepts SQL or an asserted dependency/proof from a caller.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache

from .contracts import KernelError
from .types import canonical, digest

_FORMAT = "ai-accounting-kernel/2/dashboard-projection-store/2"
_TABLES = re.compile(r'\b(?:FROM|JOIN)\s+["`\[]?([a-zA-Z_][\w]*)', re.I)
_IGNORED = {
    "state",
    "audit",
    "request",
    "jobs",
    "source_change",
    "source_change_head",
    "sqlite_schema",
    "schema_history",
    "schema_draft_history",
    "schema_identity",
}
_SOURCE_TABLES = {
    "fact": {"fact_current", "fact_revision", "fact_scope"},
    "calculation": {
        "calculation_current",
        "calculation",
        "calculation_scope",
        "monthly_account",
        "voucher",
        "voucher_version",
        "voucher_current",
        "voucher_line",
        "settlement_projection",
        "report_projection",
    },
    "publication": {
        "calculation_publication",
        "publication_current",
        "voucher_version",
        "monthly_account",
        "voucher_line",
    },
    "pending": {"pending"},
    "duplicate": {"business_duplicate_check"},
    "identity": {"identity_correction_item", "fact_current", "calculation_current"},
    "inventory": {"material_revision"},
    "inventory_item": {"material_item"},
    "close": {"period_close", "close_reference", "monthly_account"},
    "evidence": {"evidence"},
    "company_note": {"company_note_revision"},
    "display_profile": {"display_profile_revision"},
    "entity_profile": {"entity", "entity_profile_revision", "entity_resolution"},
    "commentary": {"period_commentary_revision", "period_commentary_basis"},
}


def supported(connection):
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='dashboard_projection_request'"
        ).fetchone()
        is not None
    )


def sequence(connection):
    row = connection.execute("SELECT sequence FROM source_change_head WHERE id=1").fetchone()
    return row[0] if row else 0


def _queue(connection, period, target, *, priority=0):
    connection.execute(
        "INSERT INTO dashboard_projection_request(period,target_sequence,status,priority) "
        "VALUES(?,?,'pending',?) ON CONFLICT(period) DO UPDATE SET "
        "target_sequence=max(target_sequence,excluded.target_sequence),"
        "priority=max(priority,excluded.priority),"
        "status=CASE WHEN status='running' THEN status ELSE 'pending' END,"
        "error_code=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (period, target, priority),
    )


def _event_tables(connection, after, through):
    sources = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT source FROM source_change WHERE sequence>? AND sequence<=?",
            (after, through),
        )
    }
    if any(source not in _SOURCE_TABLES for source in sources):
        return None  # new typed sources must be checked, never assumed irrelevant
    return set().union(*(_SOURCE_TABLES[source] for source in sources)) if sources else set()


def record_write(engine, connection, *, periods, before_sequence, action, lanes):
    if not supported(connection) or action.startswith("dashboard_projection_"):
        return
    target = sequence(connection)
    for period in set(periods):
        _queue(connection, period, target)
    if not lanes:
        return
    affected = _event_tables(connection, before_sequence, target)
    # Metadata mutations also invalidate selectors if they have no journal event.
    if target == before_sequence and action != "evidence":
        affected = None
    for row in connection.execute(
        "SELECT p.period,p.dependencies FROM dashboard_projection_current c "
        "JOIN dashboard_projection_publication p ON p.id=c.publication_id"
    ).fetchall():
        from .stored_json import loads_unique

        dependencies = loads_unique(row["dependencies"])
        used = set(dependencies["tables"])
        if affected is None or used & affected:
            _queue(connection, row["period"], target)


def _fingerprint(connection, sql):
    if not sql.lstrip().upper().startswith(("SELECT ", "WITH ")):
        raise KernelError("content_integrity_failed", "看板依赖不是内核读取")
    cursor = connection.execute(sql)
    hasher = hashlib.sha256(canonical([item[0] for item in cursor.description]).encode())
    for row in cursor:
        value = [
            {"blob": item.hex()}
            if isinstance(item, bytes)
            else {"float": item.hex()}
            if isinstance(item, float)
            else item
            for item in row
        ]
        raw = canonical(value).encode()
        hasher.update(len(raw).to_bytes(8, "big"))
        hasher.update(raw)
    return hasher.hexdigest()


def _basis(engine, connection, period):
    from .dashboard_model_runtime import model_scope

    scope = model_scope(engine, connection, period).model_dump(mode="python")
    scope["projection_generation"] = connection.execute(
        "SELECT coalesce(max(sequence),0) FROM dashboard_projection_reset"
    ).fetchone()[0]
    return {key: value for key, value in scope.items() if key != "epochs"}


def _dependency_summary(dependencies):
    return {
        "tables": sorted(set().union(*(set(item["tables"]) for item in dependencies))),
        "selector_digest": digest(dependencies).hex(),
        "count": len(dependencies),
    }


def _read_dependencies(derived, publication):
    from .stored_json import loads_unique

    row = derived.execute(
        "SELECT payload FROM projection_dependency WHERE model_id=?", (publication["model_id"],)
    ).fetchone()
    if row is None:
        raise KernelError("content_integrity_failed", "看板实际依赖内容缺失")
    dependencies = loads_unique(row[0])
    if _dependency_summary(dependencies) != loads_unique(publication["dependencies"]):
        raise KernelError("content_integrity_failed", "看板依赖内容与锚点不符")
    return dependencies


def _unchanged(engine, connection, publication, *, verify_selectors=True, dependencies=None):
    from .stored_json import loads_unique

    basis, summary = (
        loads_unique(publication["basis"]),
        loads_unique(publication["dependencies"]),
    )
    if digest([basis, summary]) != publication["input_digest"]:
        raise KernelError("content_integrity_failed", "看板依赖锚点损坏")
    if basis != _basis(engine, connection, publication["period"]):
        return False
    if not verify_selectors:
        current = sequence(connection)
        validated = publication["validated_sequence"]
        if current == validated:
            return True
        affected = _event_tables(connection, validated, current)
        used = set(summary["tables"])
        return affected is not None and not affected & used
    if dependencies is None:
        with _derived_store(engine, generation=basis["projection_generation"]) as produced:
            derived, store_id = produced
            if store_id != publication["store_id"]:
                raise KernelError("content_integrity_failed", "看板依赖属于另一内容库")
            dependencies = _read_dependencies(derived, publication)
    if _dependency_summary(dependencies) != summary:
        raise KernelError("content_integrity_failed", "看板依赖摘要不符")
    return all(_fingerprint(connection, item["sql"]) == item["digest"] for item in dependencies)


def _store_path(engine, *, generation=None):
    if generation is None:
        with engine.store.connection(read_only=True) as connection:
            generation = connection.execute(
                "SELECT coalesce(max(sequence),0) FROM dashboard_projection_reset"
            ).fetchone()[0]
    suffix = f".read-model-{generation}.sqlite" if generation else ".read-model.sqlite"
    return engine.store.path.with_name(engine.store.path.stem + suffix)


def _derived_schema_sql():
    from .dashboard_read_model import DASHBOARD_READ_MODEL_DDL

    return (
        "CREATE TABLE projection_identity(format TEXT NOT NULL,"
        "company_id TEXT NOT NULL,database_id TEXT NOT NULL,store_id TEXT NOT NULL,"
        "structure_digest TEXT NOT NULL) STRICT;"
        "CREATE TABLE audit(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,"
        "action TEXT NOT NULL,payload TEXT NOT NULL) STRICT;"
        "CREATE TABLE request(id TEXT PRIMARY KEY,digest BLOB NOT NULL,"
        "result TEXT NOT NULL) STRICT;"
        "CREATE TABLE projection_dependency(model_id BLOB PRIMARY KEY,"
        "payload TEXT NOT NULL CHECK(json_valid(payload))) STRICT;"
        "CREATE TRIGGER immutable_projection_dependency_update "
        "BEFORE UPDATE ON projection_dependency BEGIN "
        "SELECT RAISE(ABORT,'immutable projection dependency'); END;"
        "CREATE TRIGGER immutable_projection_dependency_delete "
        "BEFORE DELETE ON projection_dependency BEGIN "
        "SELECT RAISE(ABORT,'immutable projection dependency'); END;" + DASHBOARD_READ_MODEL_DDL
    )


@lru_cache(maxsize=1)
def _derived_structure_digest():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(_derived_schema_sql())
        return _structure(connection)


@contextmanager
def _derived_store(engine, *, create=False, generation=None):
    path = _store_path(engine, generation=generation)
    if not path.exists() and not create:
        raise KernelError("content_integrity_failed", "已发布看板内容库缺失")
    from .permissions import ensure_private_file

    ensure_private_file(path, create=create)
    if create:
        connection = sqlite3.connect(path, timeout=30)
    else:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if create:
            connection.execute("PRAGMA synchronous=FULL")
            tables = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
            if not tables:
                connection.executescript(_derived_schema_sql())
                structure = _structure(connection)
                connection.execute(
                    "INSERT INTO projection_identity VALUES(?,?,?,?,?)",
                    (
                        _FORMAT,
                        engine.store.company_id,
                        engine.store.database_id,
                        uuid.uuid4().hex,
                        structure,
                    ),
                )
                connection.commit()
        identity = connection.execute("SELECT * FROM projection_identity").fetchall()
        if (
            len(identity) != 1
            or tuple(identity[0][:3])
            != (
                _FORMAT,
                engine.store.company_id,
                engine.store.database_id,
            )
            or identity[0]["structure_digest"] != _structure(connection)
            or identity[0]["structure_digest"] != _derived_structure_digest()
        ):
            raise KernelError("content_integrity_failed", "看板内容库身份或结构不符")
        yield connection, identity[0]["store_id"]
    except sqlite3.DatabaseError as exc:
        raise KernelError("content_integrity_failed", "看板内容库损坏") from exc
    finally:
        connection.close()


def _structure(connection):
    return digest(
        [
            tuple(row)
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE sql IS NOT NULL ORDER BY type,name"
            )
        ]
    ).hex()


def _publication(connection, period):
    row = connection.execute(
        "SELECT p.*,c.validated_sequence FROM dashboard_projection_current c "
        "JOIN dashboard_projection_publication p ON p.id=c.publication_id WHERE c.period=?",
        (period,),
    ).fetchone()
    latest = connection.execute(
        "SELECT id FROM dashboard_projection_publication WHERE period=? AND rowid>coalesce("
        "(SELECT max(publication_through) FROM dashboard_projection_reset),0) "
        "ORDER BY rowid DESC LIMIT 1",
        (period,),
    ).fetchone()
    if latest is not None and (row is None or row["id"] != latest[0]):
        raise KernelError("content_integrity_failed", "看板发布定位与保留历史不符")
    return row


def reset_after_restore(connection, *, reason="restore"):
    """Explicit restore boundary preserves old proofs while scheduling new derived bytes."""
    if not supported(connection):
        return
    connection.execute(
        "INSERT INTO dashboard_projection_reset(sequence,publication_through,reason) "
        "SELECT coalesce((SELECT max(sequence) FROM dashboard_projection_reset),0)+1,"
        "coalesce((SELECT max(rowid) FROM dashboard_projection_publication),0),?",
        (reason,),
    )
    connection.execute("DELETE FROM dashboard_projection_current")
    connection.execute(
        "UPDATE dashboard_projection_request SET status='pending',worker_id=NULL,"
        "lease_until_ms=0,error_code=NULL,priority=0"
    )


def _anchored_model(engine, publication, derived, store_id):
    from .dashboard_read_model import LoadedModel, _anchored_descriptor, _load_root, _receipt_row

    if publication["store_id"] != store_id:
        raise KernelError("content_integrity_failed", "看板发布属于另一内容库")
    row = _receipt_row(derived, publication["model_id"])
    if row is None:
        raise KernelError("content_integrity_failed", "已发布看板内容缺失")
    descriptor = _anchored_descriptor(row)
    root = _load_root(derived, descriptor)
    if (
        bytes.fromhex(descriptor.root_digest) != publication["root_digest"]
        or descriptor.scope.company_id != engine.store.company_id
        or descriptor.scope.database_id != engine.store.database_id
        or descriptor.scope.period != publication["period"]
    ):
        raise KernelError("content_integrity_failed", "看板内容与发布锚点不符")
    if (
        publication["id"]
        != digest(
            [
                store_id,
                descriptor.model_dump(mode="python"),
                publication["source_sequence"],
                bytes(publication["input_digest"]).hex(),
            ]
        ).hex()
    ):
        raise KernelError("content_integrity_failed", "看板发布摘要与内容不符")
    model = LoadedModel(derived, descriptor, root)
    _read_dependencies(derived, publication)
    for reference in root.blocks:
        model.read_page_block(reference.key)
    return model


def load_for_snapshot(engine, snap):
    publication = _publication(snap.connection, snap.period)
    if publication is None:
        return None
    # The source sequence is only a cheap change locator, never a content version.
    if not _unchanged(engine, snap.connection, publication, verify_selectors=False):
        return None
    from .stored_json import loads_unique

    generation = loads_unique(publication["basis"])["projection_generation"]
    manager = _derived_store(engine, generation=generation)
    connection, store_id = manager.__enter__()
    try:
        connection.execute("BEGIN")
        model = _anchored_model(engine, publication, connection, store_id)
    except BaseException:
        manager.__exit__(None, None, None)
        raise
    snap.semantic_cache.setdefault("derived_resources", []).append(manager)
    snap.snapshot_version = publication["id"]
    return model


def delivery_state(engine, period):
    """Authenticate content before readiness; stale and damaged are distinct."""
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        if not supported(connection):
            raise KernelError("schema_migration_required", "当前公司库需要显式前向结构调整")
        try:
            publication = _publication(connection, period)
        except KernelError:
            return {"state": "failed", "model_version": None, "reason": "corrupt"}
        request = connection.execute(
            "SELECT status,error_code FROM dashboard_projection_request WHERE period=?",
            (period,),
        ).fetchone()
        if request and request["status"] == "failed":
            return {
                "state": "failed",
                "model_version": None,
                "reason": "corrupt"
                if request["error_code"] == "content_integrity_failed"
                else "production_failed",
            }
        if publication is not None:
            try:
                from .stored_json import loads_unique

                generation = loads_unique(publication["basis"])["projection_generation"]
                with _derived_store(engine, generation=generation) as (derived, store_id):
                    derived.execute("BEGIN")
                    _anchored_model(engine, publication, derived, store_id)
                if _unchanged(engine, connection, publication, verify_selectors=False):
                    return {"state": "ready", "model_version": publication["id"]}
            except KernelError as exc:
                if exc.code == "content_integrity_failed":
                    return {"state": "failed", "model_version": None, "reason": "corrupt"}
                raise
        target = sequence(connection)
    engine._write(
        "projection-request/" + uuid.uuid4().hex,
        digest(["projection-request", period, target]),
        None,
        (),
        "dashboard_projection_request",
        lambda connection: (
            _queue(connection, period, target, priority=100)
            or {"status": "queued", "period": period}
        ),
    )
    return {"state": "preparing", "model_version": None, "retry_after_ms": 500}


def _produce(engine, period, *, worker_id=None):
    from .dashboard import Dashboard, _Snapshot
    from .dashboard_model_materializer import build_model
    from .dashboard_model_runtime import _producing, model_scope, stable_payload
    from .dashboard_read_model import PageBlock, persist_model, prepare_model
    from .query_reads import QueryReads

    statements = set()
    started = time.perf_counter()
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        authoritative = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        } - _IGNORED
        authoritative = {
            name
            for name in authoritative
            if not name.startswith(
                ("dashboard_read_model", "dashboard_projection", "replay_", "backup_")
            )
        }

        def trace(sql):
            if sql.lstrip().upper().startswith(("SELECT ", "WITH ")):
                tables = set(_TABLES.findall(sql)) & authoritative
                # Snapshot envelope versions do not describe displayed content.
                if tables and not re.fullmatch(
                    r"SELECT period FROM period_close(?: ORDER BY period)?", sql, re.I
                ):
                    statements.add(sql)

        dashboard = Dashboard(engine)

        @contextmanager
        def snapshot(requested):
            snap = _Snapshot(engine, connection, requested, reads)
            try:
                yield snap
            finally:
                snap.release()

        dashboard._snapshot = snapshot
        token = _producing.set(True)
        connection.set_trace_callback(trace)
        try:
            content = build_model(dashboard, period, default_only=True)
        finally:
            connection.set_trace_callback(None)
            _producing.reset(token)
        dependencies = [
            {
                "sql": sql,
                "tables": sorted(set(_TABLES.findall(sql)) & authoritative),
                "digest": _fingerprint(connection, sql),
            }
            for sql in sorted(statements)
        ]
        basis, target = _basis(engine, connection, period), sequence(connection)
        scope = model_scope(engine, connection, period)
    # Durable bytes precede the publication. Orphans after a crash are harmless.
    with _derived_store(engine, create=True, generation=basis["projection_generation"]) as produced:
        derived, store_id = produced
        derived.execute("BEGIN IMMEDIATE")
        current = derived.execute(
            "SELECT commit_id FROM dashboard_read_model_current WHERE period=?",
            (period,),
        ).fetchone()
        request_id = "dashboard-read-model/" + uuid.uuid4().hex
        blocks = [
            PageBlock(
                block.key,
                block.kind,
                stable_payload(block.payload),
                keys=block.keys,
                sources=block.sources,
                voucher_version_ids=block.voucher_version_ids,
                total_count=block.total_count,
                filtered_count=block.filtered_count,
                item_sources=block.item_sources,
                item_voucher_version_ids=block.item_voucher_version_ids,
                fact_sources=block.fact_sources,
                item_fact_sources=block.item_fact_sources,
            )
            for block in content.blocks
        ]
        prepared = prepare_model(
            scope,
            blocks,
            request_id=request_id,
            parent_commit_id=bytes(current[0]).hex() if current else None,
        )
        result = {"status": "materialized", "dashboard_models": [prepared.descriptor]}
        audit = derived.execute(
            "INSERT INTO audit(request_id,action,payload) VALUES(?,?,?)",
            (request_id, "dashboard_read_model", canonical(result)),
        )
        derived.execute(
            "INSERT INTO request VALUES(?,?,?)",
            (
                request_id,
                digest(["dashboard_read_model", result]),
                canonical(result),
            ),
        )
        persist_model(derived, prepared, audit_id=audit.lastrowid, collect_obsolete=False)
        derived.execute(
            "INSERT INTO projection_dependency VALUES(?,?)",
            (
                bytes.fromhex(prepared.descriptor["commit_id"]),
                canonical(dependencies),
            ),
        )
        derived.commit()
    descriptor = prepared.descriptor
    summary = _dependency_summary(dependencies)
    input_digest = digest([basis, summary])
    ident = digest([store_id, descriptor, target, input_digest.hex()]).hex()
    expected = {
        "basis": canonical(basis),
        "dependencies": canonical(summary),
        "input_digest": input_digest,
        "period": period,
    }
    # Slow selector validation uses its own consistent read snapshot. Only the
    # journal compare-and-swap remains inside the publication write transaction.
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        verified = _unchanged(engine, connection, expected, dependencies=dependencies)
        verified_sequence = sequence(connection)

    def publish(connection):
        if (
            worker_id is not None
            and not connection.execute(
                "SELECT 1 FROM dashboard_projection_request "
                "WHERE period=? AND worker_id=? AND status='running'",
                (period, worker_id),
            ).fetchone()
        ):
            return {"status": "superseded", "period": period}
        engine.fault("dashboard_before_publish", connection)
        if (
            not verified
            or sequence(connection) != verified_sequence
            or basis != _basis(engine, connection, period)
        ):
            _queue(connection, period, sequence(connection))
            connection.execute(
                "UPDATE dashboard_projection_request SET status='pending',worker_id=NULL,"
                "lease_until_ms=0 WHERE period=?",
                (period,),
            )
            return {"status": "superseded", "period": period}
        connection.execute(
            "INSERT INTO dashboard_projection_publication"
            "(id,period,store_id,model_id,root_digest,source_sequence,"
            "basis,dependencies,input_digest) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                ident,
                period,
                store_id,
                bytes.fromhex(descriptor["commit_id"]),
                bytes.fromhex(descriptor["root_digest"]),
                target,
                canonical(basis),
                canonical(summary),
                input_digest,
            ),
        )
        connection.execute(
            "INSERT INTO dashboard_projection_current VALUES(?,?,?) "
            "ON CONFLICT(period) DO UPDATE SET "
            "publication_id=excluded.publication_id,validated_sequence=excluded.validated_sequence",
            (period, ident, sequence(connection)),
        )
        connection.execute(
            "UPDATE dashboard_projection_request SET status='ready',priority=0,"
            "worker_id=NULL,lease_until_ms=0,error_code=NULL WHERE period=?",
            (period,),
        )
        return {
            "status": "ready",
            "period": period,
            "model_version": ident,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    return engine._write(
        "projection-publish/" + ident,
        digest([ident, "publish"]),
        None,
        (),
        "dashboard_projection_publish",
        publish,
    )


def run_pending(engine, *, limit=1):
    """A bounded worker pass; replay merges historical production until its stage ends."""
    results = []
    for _ in range(limit):
        now, worker = int(time.time() * 1000), uuid.uuid4().hex

        def claim(connection, *, now=now, worker=worker):
            if not supported(connection):
                return {"status": "idle"}
            replay = connection.execute(
                "SELECT 1 FROM replay_run WHERE status IN('pending','running') LIMIT 1"
            ).fetchone()
            row = connection.execute(
                "SELECT period FROM dashboard_projection_request WHERE "
                "(status='pending' OR (status='running' AND lease_until_ms<?)) "
                + ("AND priority>0 " if replay else "")
                + "ORDER BY priority DESC,period DESC LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return {"status": "idle"}
            connection.execute(
                "UPDATE dashboard_projection_request SET status='running',worker_id=?,"
                "lease_until_ms=?,attempts=attempts+1 WHERE period=?",
                (worker, now + 300000, row[0]),
            )
            return {"status": "claimed", "period": row[0]}

        claimed = engine._write(
            "projection-claim/" + worker,
            digest([worker, "claim"]),
            None,
            (),
            "dashboard_projection_claim",
            claim,
        )
        if claimed["status"] == "idle":
            break
        period = claimed["period"]
        try:
            with engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                publication = _publication(connection, period)
                reusable = publication is not None and _unchanged(engine, connection, publication)
                verified_sequence = sequence(connection)
            if publication is not None:
                from .stored_json import loads_unique

                generation = loads_unique(publication["basis"])["projection_generation"]
                with _derived_store(engine, generation=generation) as (derived, store_id):
                    derived.execute("BEGIN")
                    _anchored_model(engine, publication, derived, store_id)
            if reusable:

                def refresh(
                    connection,
                    *,
                    period=period,
                    publication=publication,
                    verified_sequence=verified_sequence,
                ):
                    current = _publication(connection, period)
                    if (
                        current is None
                        or current["id"] != publication["id"]
                        or sequence(connection) != verified_sequence
                        or canonical(_basis(engine, connection, period)) != publication["basis"]
                    ):
                        _queue(connection, period, sequence(connection))
                        connection.execute(
                            "UPDATE dashboard_projection_request "
                            "SET status='pending' WHERE period=?",
                            (period,),
                        )
                        return {"status": "superseded", "period": period}
                    connection.execute(
                        "UPDATE dashboard_projection_current "
                        "SET validated_sequence=? WHERE period=?",
                        (sequence(connection), period),
                    )
                    connection.execute(
                        "UPDATE dashboard_projection_request SET status='ready',priority=0,"
                        "worker_id=NULL,lease_until_ms=0 WHERE period=?",
                        (period,),
                    )
                    return {"status": "reused", "period": period}

                outcome = engine._write(
                    "projection-refresh/" + worker,
                    digest([worker, "refresh"]),
                    None,
                    (),
                    "dashboard_projection_refresh",
                    refresh,
                )
            else:
                outcome = _produce(engine, period, worker_id=worker)
            results.append(outcome)
        except Exception as exc:
            code = getattr(exc, "code", "production_failed")

            def failed(connection, *, code=code, period=period, worker=worker):
                connection.execute(
                    "UPDATE dashboard_projection_request SET status='failed',error_code=?,"
                    "worker_id=NULL,lease_until_ms=0 WHERE period=? AND worker_id=?",
                    (code, period, worker),
                )
                return {"status": "failed", "period": period, "error_code": code}

            results.append(
                engine._write(
                    "projection-failed/" + worker,
                    digest([worker, code]),
                    None,
                    (),
                    "dashboard_projection_failed",
                    failed,
                )
            )
    return results
