"""Independent complete checks of derived owner content and its production."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext

from .contracts import KernelError
from .dashboard_model_runtime import (
    _independent_outer_read,
    _producing,
    model_scope,
    stable_payload,
    supports_model,
)
from .dashboard_read_model import (
    LoadedModel,
    _anchored_descriptor,
    _load_root,
    _verified_audit_descriptors,
    supports_native_content,
)
from .storage import _active_fact_reads
from .types import canonical


def _invalid():
    return KernelError("content_integrity_failed", "看板生产记录或独立重建核验不一致",
                       component="dashboard_read_model")


@contextmanager
def authority_snapshot(engine, connection, period, *, as_of):
    """Use raw domain readers on the verifier's existing transaction."""
    from .dashboard import _Snapshot
    from .query_reads import QueryReads
    from .verified_source_lease import require_verified_lease, verified_source_lease
    from .verified_source_reads import readonly_verification

    if not connection.in_transaction:
        raise _invalid()
    active = _active_fact_reads.get()
    if active is not None and active.connection is not connection:
        if _producing.get() or not _independent_outer_read(active, connection):
            raise _invalid()
        # Reconstruct in this verifier's transaction, not the unrelated reader.
        # The ContextVar token below restores that reader without touching it.
        active = None
    if active is not None and (
        active.connection is not connection or active.engine is not engine
        or not active._snapshot_active
    ):
        raise _invalid()
    # A nested owned/production reader already has its own authorizer. Do not
    # replace it or issue a setter that its guard deliberately prohibits.
    protected = connection.execute("PRAGMA query_only").fetchone()[0] == 1
    protection = nullcontext() if protected else readonly_verification(connection)
    witness_scope = nullcontext(None) if active is not None else verified_source_lease(connection)
    with protection, witness_scope as witness:
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise _invalid()
        changes = connection.total_changes
        schema = connection.execute("PRAGMA schema_version").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        reads = active or QueryReads(engine, connection)
        token = None
        if active is None:
            reads._snapshot_active = True
            token = _active_fact_reads.set(reads)
        snapshot_token = reads._snapshot_token
        producing = _producing.set(True)
        snap = None
        try:
            snap = _Snapshot(engine, connection, period, reads)
            snap.as_of = as_of
            yield snap
            if (
                not connection.in_transaction or connection.total_changes != changes
                or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                or connection.execute("PRAGMA schema_version").fetchone()[0] != schema
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != foreign_keys
                or _active_fact_reads.get() is not reads or not reads._snapshot_active
                or reads._snapshot_token is not snapshot_token
            ):
                raise _invalid()
            if witness is not None:
                try:
                    require_verified_lease(connection, witness)
                except ValueError as error:
                    raise _invalid() from error
        finally:
            if snap is not None:
                snap.release()
            _producing.reset(producing)
            if token is not None:
                _active_fact_reads.reset(token)
                reads._reset()


def _semantic_block(block):
    # This is the one deliberately volatile display timestamp. Actual event
    # dates, source recording times, versions and business states are retained.
    payload = stable_payload(block.payload)
    return canonical({
        "kind": block.kind, "payload": payload, "keys": block.keys,
        "sources": block.sources, "voucher_version_ids": block.voucher_version_ids,
        "item_sources": block.item_sources,
        "item_voucher_version_ids": block.item_voucher_version_ids,
        "fact_sources": block.fact_sources, "item_fact_sources": block.item_fact_sources,
        "total_count": block.total_count, "filtered_count": block.filtered_count,
    })


def _verify_employee_dates(engine, connection, model, *, current_day):
    """Compare certified personnel adaptation with raw rows at each real boundary.

    This belongs only to independent full verification. Normal reads consume
    the root's complete typed directory and authenticate their returned hits.
    Every date uses a fresh domain snapshot in the verifier's owned transaction.
    """
    from .dashboard import _employees
    from .dashboard_model_dates import date_verification_days, employee_states
    from .dashboard_model_filters import directory_matches

    basis = model.root.date_basis
    if basis is None:
        return
    refs = [reference for reference in model.root.blocks
            if reference.key.startswith("employees/all/employees/")]
    saved_items = {}
    entries = []
    for reference in refs:
        block = model.read_page_block(reference.key)
        entries.extend(reference.keys)
        for item in block.payload["data"]["collections"]["employees"]["items"]:
            if item["employee_id"] in saved_items:
                raise _invalid()
            saved_items[item["employee_id"]] = item
    days = set(date_verification_days(basis)) | {str(current_day)}
    for day in sorted(days):
        with authority_snapshot(engine, connection, model.root.scope.period, as_of=day) as snap:
            native_items, after = [], None
            while True:
                data = _employees(snap, sections={"employees"}, employee_filter="all",
                                  cursors={"employees": after} if after else None, limit=500)
                collection = data["collections"]["employees"]
                native_items.extend(collection["items"])
                if not collection["page"]["has_more"]:
                    break
                following = collection["page"]["next_cursor"]
                if following is None or following == after:
                    raise _invalid()
                after = following
            states = employee_states(basis, day)
            adapted = [{**saved_items[entry[0]], "employment_state": states[entry[0]]}
                       for entry in entries]
            if adapted != native_items:
                raise _invalid()
            for employee_filter in (
                "all", "in_period", "unknown", "ended", "employment_active",
                "employment_unpaid_leave", "employment_departed", "employment_unknown",
            ):
                filters = {"employee_filter": employee_filter, "employee_id": None}
                selected = [entry[0] for entry in entries if directory_matches(
                    "employees", "employees", entry, filters, employee_states=states,
                )]
                # Raw row fields independently carry period membership and the
                # evaluated current state; no model predicate supplies them.
                expected = [item["employee_id"] for item in native_items if (
                    employee_filter == "all"
                    or employee_filter == "in_period" and item.get("in_period") is True
                    or employee_filter == "unknown" and (
                        item.get("period_state") == "unknown"
                        or item["selection_status"] == "unestablished"
                    )
                    or employee_filter == "ended" and item.get("period_state") == "ended"
                    or employee_filter == "employment_active"
                    and item["employment_state"] == "regular"
                    or employee_filter.startswith("employment_")
                    and employee_filter != "employment_active"
                    and item["employment_state"] == employee_filter.removeprefix("employment_")
                )]
                if selected != expected:
                    raise _invalid()


def verify_models(engine, connection, *, compare_content=True, reconstruct_current=True):
    """Run after the complete authoritative source verification, not instead.

    All production receipts and chains are checked; required current bytes are
    checked independently. Obsolete UI bytes may be reclaimed. Only a scope that
    can serve current reads is compared with an independent raw reconstruction;
    obsolete UI models never replace frozen accounting history.
    """
    if not supports_model(connection):
        return {"status": "not_applicable"}
    commits = {}
    # An unowned autocommit reader retains independent proofs for each row.
    verified_batches = {} if connection.in_transaction else None
    rows = connection.execute(
        "SELECT c.*,a.request_id anchor_request_id,a.action anchor_action,"
        "a.payload anchor_payload,r.result request_result,r.digest request_digest "
        "FROM dashboard_read_model_commit c LEFT JOIN audit a ON a.id=c.audit_id "
        "LEFT JOIN request r ON r.id=c.request_id ORDER BY c.audit_id,c.period"
    )
    for row in rows:
        descriptor = _anchored_descriptor(row, _verified_batches=verified_batches)
        if (descriptor.scope.company_id, descriptor.scope.database_id) != (
            engine.store.company_id, engine.store.database_id,
        ):
            raise _invalid()
        commits[descriptor.commit_id] = (row["audit_id"], descriptor)
    audit_members = set()
    for row in connection.execute(
        "SELECT a.*,r.result request_result,r.digest request_digest FROM audit a "
        "LEFT JOIN request r ON r.id=a.request_id WHERE a.action='dashboard_read_model'"
    ):
        for descriptor in _verified_audit_descriptors(
            row["id"], row["request_id"], row["action"], row["payload"],
            row["request_result"], row["request_digest"], verified_batches,
        ).values():
            member = commits.get(descriptor.commit_id)
            if member != (row["id"], descriptor) or descriptor.commit_id in audit_members:
                raise _invalid()
            audit_members.add(descriptor.commit_id)
    if audit_members != commits.keys():
        raise _invalid()
    latest, loaded = {}, {}
    for ident, (_audit_id, descriptor) in commits.items():
        previous = latest.get(descriptor.scope.period)
        if descriptor.parent_commit_id != previous:
            raise _invalid()
        latest[descriptor.scope.period] = ident
    current = {row[0]: bytes(row[1]).hex() for row in connection.execute(
        "SELECT period,commit_id FROM dashboard_read_model_current"
    )}
    if compare_content and current != latest:
        raise _invalid()
    if compare_content:
        for ident in current.values():
            descriptor = commits[ident][1]
            root = _load_root(connection, descriptor)
            model = LoadedModel(connection, descriptor, root)
            native = supports_native_content(descriptor)
            for reference in root.blocks:
                model.read_page_block(reference.key, native_contract=native)
            loaded[ident] = model
    compared = 0
    if compare_content and reconstruct_current:
        from .dashboard import Dashboard
        from .dashboard_model_materializer import build_model

        for period, ident in current.items():
            model = loaded[ident]
            scope = model.descriptor.scope
            current_scope = model_scope(engine, connection, period)
            if not supports_native_content(model.descriptor) or scope != current_scope.model_copy(
                update={"as_of": scope.as_of},
            ) or (model.root.date_basis is None and scope.as_of != current_scope.as_of):
                continue  # Never serve this scope; the original path remains authoritative.
            dashboard = Dashboard(engine)

            @contextmanager
            def snapshot(requested_period, scope=scope):
                with authority_snapshot(engine, connection, requested_period,
                                        as_of=scope.as_of) as snap:
                    yield snap

            dashboard._snapshot = snapshot
            rebuilt = build_model(dashboard, period)
            expected = {block.key: _semantic_block(block) for block in rebuilt.blocks}
            observed = {reference.key: _semantic_block(model.read_page_block(reference.key))
                        for reference in model.root.blocks}
            if observed != expected or model.root.date_basis != rebuilt.date_basis:
                raise _invalid()
            _verify_employee_dates(engine, connection, model, current_day=current_scope.as_of)
            compared += 1
    return {"status": "verified", "receipts": len(commits), "current_compared": compared,
            "derived_content": "verified" if compare_content else "not_checked"}


def repair_models(engine, connection):
    """Restore derived current bytes only when independent reconstruction agrees.

    Production receipts are immutable. Required current UI bytes must match
    independent reconstruction; retired UI bytes need no reconstruction.
    Financial frozen history is never a model repair target.
    """
    import hashlib

    from .dashboard import Dashboard
    from .dashboard_model_materializer import build_model
    from .dashboard_model_runtime import stable_payload
    from .dashboard_read_model import PageBlock, prepare_model

    if not supports_model(connection):
        return False
    verify_models(engine, connection, compare_content=False)
    changed = False
    rows = list(connection.execute(
        "SELECT c.* FROM dashboard_read_model_commit c WHERE c.audit_id=("
        "SELECT max(q.audit_id) FROM dashboard_read_model_commit q WHERE q.period=c.period)"
    ))
    from .dashboard_read_model import _parse_descriptor

    for row in rows:
        descriptor = _parse_descriptor(row["descriptor"])
        if not supports_native_content(descriptor):
            continue  # Replace an older UI contract; never recertify its original bytes.
        scope = descriptor.scope
        current_scope = model_scope(engine, connection, scope.period, as_of=scope.as_of)
        if scope != current_scope:
            # An authentic stale model cannot serve a request. The next real
            # production creates new content; no old historical bytes are edited.
            continue
        dashboard = Dashboard(engine)

        @contextmanager
        def snapshot(period, scope=scope):
            with authority_snapshot(engine, connection, period, as_of=scope.as_of) as snap:
                yield snap

        dashboard._snapshot = snapshot
        rebuilt = build_model(dashboard, scope.period)
        blocks = [PageBlock(
            block.key, block.kind, stable_payload(block.payload), keys=block.keys,
            sources=block.sources, voucher_version_ids=block.voucher_version_ids,
            total_count=block.total_count, filtered_count=block.filtered_count,
            item_sources=block.item_sources,
            item_voucher_version_ids=block.item_voucher_version_ids,
            fact_sources=block.fact_sources, item_fact_sources=block.item_fact_sources,
        ) for block in rebuilt.blocks]
        prepared = prepare_model(scope, blocks, request_id=descriptor.request_id,
                                 parent_commit_id=descriptor.parent_commit_id,
                                 date_basis=rebuilt.date_basis)
        if prepared.descriptor != descriptor.model_dump(mode="python"):
            raise _invalid()
        for block_digest, raw in prepared.block_rows:
            found = connection.execute(
                "SELECT payload FROM dashboard_read_model_block WHERE digest=?", (block_digest,)
            ).fetchone()
            if found is None or found[0] != raw:
                if hashlib.sha256(raw.encode()).digest() != block_digest:
                    raise _invalid()
                connection.execute(
                    "INSERT INTO dashboard_read_model_block(digest,payload) VALUES(?,?) "
                    "ON CONFLICT(digest) DO UPDATE SET payload=excluded.payload",
                    (block_digest, raw),
                )
                changed = True
        locator = connection.execute(
            "SELECT commit_id FROM dashboard_read_model_current WHERE period=?", (scope.period,)
        ).fetchone()
        if locator is None or bytes(locator[0]) != bytes(row["id"]):
            connection.execute(
                "INSERT INTO dashboard_read_model_current(period,commit_id) VALUES(?,?) "
                "ON CONFLICT(period) DO UPDATE SET commit_id=excluded.commit_id",
                (scope.period, row["id"]),
            )
            changed = True
    # Retired UI frames are not financial history and need no reconstruction.
    # Current exact content was independently reconstructed above already.
    verify_models(engine, connection, reconstruct_current=False)
    return changed
