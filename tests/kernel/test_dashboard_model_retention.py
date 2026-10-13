"""Current UI bytes are required; historical production receipts remain immutable."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_banking import book as book  # noqa: F401
from test_banking import funding, opening
from test_dashboard_read_model_storage import anchor, assert_corrupt, produce
from test_dashboard_read_model_storage import database as database  # noqa: F401
from test_dashboard_read_model_storage import native as native  # noqa: F401

from ai_accounting.kernel import dashboard_read_model as storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import _produce
from ai_accounting.kernel.dashboard_model_integrity import verify_models
from ai_accounting.kernel.engine import PROGRAM_VERSION
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.types import canonical


def verifier(scope):
    return SimpleNamespace(store=SimpleNamespace(company_id=scope.company_id,
                                                database_id=scope.database_id))


def test_three_productions_retain_receipts_and_only_current_roots(database, native):
    scope, blocks = native
    receipts = []
    for index in range(3):
        changed = scope.model_copy(update={"read_repair_revision": index})
        receipts.append(produce(database, changed, blocks, key=f"production-{index}",
                                parent=receipts[-1].descriptor["commit_id"] if receipts else None))
    assert database.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0] == 3
    assert database.execute("SELECT count(*) FROM audit").fetchone()[0] == 3
    assert database.execute("SELECT count(*) FROM request").fetchone()[0] == 3
    database.execute("BEGIN")
    live = storage.authenticate_current_storage(database)
    remaining = database.execute("SELECT count(*) FROM dashboard_read_model_block").fetchone()[0]
    assert remaining == len(live)
    for receipt in receipts[:-1]:
        assert receipt.descriptor["root_digest"] not in live
    assert storage.load_model(database, changed).read_block(blocks[0].key) == blocks[0].payload
    assert verify_models(verifier(scope), database, reconstruct_current=False)["receipts"] == 3


def test_shared_quarterly_block_survives_replacing_one_month(database, book):
    engine, save, publish, _ = book
    opening(save, publish)
    payload = Dashboard(engine).quarterly_report(2026, 3)
    from ai_accounting.kernel.dashboard_model_runtime import model_scope

    with engine.store.connection(read_only=True) as connection:
        scope = model_scope(engine, connection, "2026-09")
    block = storage.PageBlock("quarterly_report/default/overview/0",
                              "dashboard_quarterly_report", payload)
    august = scope.model_copy(update={"period": "2026-08"})
    first = produce(database, august, [block], key="august")
    produce(database, scope, [block], key="september")
    modified = copy.deepcopy(payload)
    modified["headline"] += " revised"
    new_block = storage.PageBlock(block.key, block.kind, modified)
    produce(database, scope, [new_block], key="september-revised",
            parent=database.execute("SELECT hex(commit_id) FROM dashboard_read_model_current "
                                    "WHERE period='2026-09'").fetchone()[0].lower())
    database.execute("BEGIN")
    assert storage.load_model(database, august).read_block(block.key) == payload
    assert storage.load_model(database, scope).read_block(block.key) == modified
    assert first.descriptor["root_digest"] in storage.authenticate_current_storage(database)


def test_old_interface_current_is_authenticated_then_replaced_without_old_adapter(
    database, native, monkeypatch,
):
    scope, blocks = native
    old = scope.model_copy(update={"rule_build": "old-interface-build"})
    payload = copy.deepcopy(blocks[0].payload)
    payload["schema_version"] = 8
    old_block = storage.PageBlock(blocks[0].key, blocks[0].kind, payload)
    # Simulate a legitimate earlier native producer. Its immutable original
    # bytes and receipt are then checked by the newer runtime unchanged.
    with monkeypatch.context() as previous:
        previous.setattr(storage, "_checked_payload", lambda _kind, value: value)
        first = produce(database, old, [old_block], key="old-interface")
    current_scope = scope.model_copy(update={"rule_build": PROGRAM_VERSION})
    database.execute("BEGIN")
    assert storage.load_model(database, current_scope) is None
    assert verify_models(verifier(scope), database, reconstruct_current=False)["receipts"] == 1
    database.rollback()
    produce(database, current_scope, blocks, key="new-interface",
            parent=first.descriptor["commit_id"])
    database.execute("BEGIN")
    assert verify_models(verifier(scope), database, reconstruct_current=False)["receipts"] == 2
    loaded = storage.load_model(database, current_scope)
    assert loaded.read_block(blocks[0].key) == blocks[0].payload
    assert first.descriptor["root_digest"] not in storage.authenticate_current_storage(database)


def test_new_publication_cannot_hide_damaged_stale_current(database, native):
    scope, blocks = native
    first = produce(database, scope, blocks)
    with database:
        database.execute("UPDATE dashboard_read_model_block SET payload='{}' WHERE digest=?",
                         (bytes.fromhex(first.descriptor["root_digest"]),))
    before = tuple(database.iterdump())
    assert_corrupt(lambda: produce(database, scope.model_copy(update={"read_repair_revision": 1}),
                                  blocks, key="cannot-replace-damage",
                                  parent=first.descriptor["commit_id"]))
    assert tuple(database.iterdump()) == before


def test_garbage_collection_failure_rolls_back_receipt_locator_and_deletions(
    database, native, monkeypatch,
):
    scope, blocks = native
    first = produce(database, scope, blocks)
    before = tuple(database.iterdump())
    actual_collect = storage.collect_obsolete_blocks

    def fail_after_collect(connection):
        assert actual_collect(connection) > 0
        raise RuntimeError("synthetic interrupted collection")

    monkeypatch.setattr(storage, "collect_obsolete_blocks", fail_after_collect)
    with pytest.raises(RuntimeError, match="interrupted collection"):
        produce(database, scope.model_copy(update={"read_repair_revision": 1}), blocks,
                key="failed-collection", parent=first.descriptor["commit_id"])
    assert tuple(database.iterdump()) == before


@pytest.mark.parametrize("target", ["audit", "parent"])
def test_retired_receipt_anchor_and_parent_chain_are_still_required(database, native, target):
    scope, blocks = native
    first = produce(database, scope, blocks)
    produce(database, scope.model_copy(update={"read_repair_revision": 1}), blocks,
            key="second", parent=first.descriptor["commit_id"])
    with database:
        if target == "audit":
            database.execute("UPDATE audit SET payload='{}' WHERE id=1")
        else:
            database.execute("DROP TRIGGER immutable_dashboard_read_model_commit_update")
            database.execute("UPDATE dashboard_read_model_commit SET parent_id=NULL "
                             "WHERE request_id='second'")
    database.execute("BEGIN")
    assert_corrupt(lambda: verify_models(verifier(scope), database, reconstruct_current=False))


def test_current_build_wrong_native_schema_is_not_an_interface_fallback(
    database, native, monkeypatch,
):
    scope, blocks = native
    scope = scope.model_copy(update={"rule_build": PROGRAM_VERSION})
    payload = copy.deepcopy(blocks[0].payload)
    payload["schema_version"] = 8
    bad_block = storage.PageBlock(blocks[0].key, blocks[0].kind, payload)
    with monkeypatch.context() as producer:
        producer.setattr(storage, "_checked_payload", lambda _kind, value: value)
        prepared = storage.prepare_model(scope, [bad_block], request_id="bad-current")
    with pytest.raises(KernelError) as failure:
        with database:
            audit_id = anchor(database, [prepared])
            storage.persist_model(database, prepared, audit_id=audit_id)
    assert getattr(failure.value, "code", None) == "content_integrity_failed"
    assert database.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0] == 0


def test_current_build_unknown_content_version_is_rejected(database, native, monkeypatch):
    scope, blocks = native
    scope = scope.model_copy(update={"rule_build": PROGRAM_VERSION})
    # Keep hashes and production anchors valid: the defect is an incompatible
    # contract emitted with the identical producer build, not damaged bytes.
    original = storage.prepare_model(scope, blocks, request_id="wrong-content-version")
    root = storage.loads_unique(original.root_json)
    root["content_version"] = storage.CONTENT_VERSION + 1
    root["format"] = f"ai-accounting-kernel/2/dashboard-read-model/{root['content_version']}"
    root_json = canonical(root)
    root_digest = hashlib.sha256(root_json.encode()).digest()
    descriptor = {**original.descriptor, "content_version": root["content_version"],
                  "root_digest": root_digest.hex()}
    descriptor["commit_id"] = storage._descriptor_id(descriptor)
    rows = [(ident, raw) for ident, raw in original.block_rows
            if ident.hex() != original.descriptor["root_digest"]]
    rows.append((root_digest, root_json))
    prepared = storage.PreparedModel(canonical(descriptor), root_json, tuple(rows))
    # The production helper now rejects unsupported formats immediately. Seed
    # an independently hash-consistent unknown receipt to exercise read gates.
    with database:
        audit_id = anchor(database, [prepared])
        database.execute(
            "INSERT INTO dashboard_read_model_commit VALUES(?,?,?,?,?,?,?)",
            (bytes.fromhex(descriptor["commit_id"]), scope.period, descriptor["request_id"],
             audit_id, prepared.descriptor_json, root_digest, None),
        )
        database.executemany("INSERT INTO dashboard_read_model_block VALUES(?,?)", rows)
        database.execute("INSERT INTO dashboard_read_model_current VALUES(?,?)",
                         (scope.period, bytes.fromhex(descriptor["commit_id"])))
    database.execute("BEGIN")
    assert_corrupt(lambda: storage.load_model(database, scope))
    assert_corrupt(lambda: storage.authenticate_current_storage(database))
    assert_corrupt(lambda: verify_models(verifier(scope), database, reconstruct_current=False))


def test_content_v1_receipt_remains_immutable_across_v2_forward_production(
    database, native, monkeypatch,
):
    scope, blocks = native
    with monkeypatch.context() as previous:
        previous.setattr(storage, "CONTENT_VERSION", 1)
        old = produce(database, scope, blocks, key="retained-v1")
    before = tuple(database.execute(
        "SELECT * FROM dashboard_read_model_commit WHERE request_id='retained-v1'",
    ).fetchone())
    database.execute("BEGIN")
    assert storage.load_model(database, scope) is None
    assert storage.authenticate_current_storage(database)
    database.rollback()
    new = produce(database, scope, blocks, key="forward-v2", parent=old.descriptor["commit_id"])
    assert new.descriptor["content_version"] == storage.CONTENT_VERSION
    assert new.descriptor["parent_commit_id"] == old.descriptor["commit_id"]
    assert tuple(database.execute(
        "SELECT * FROM dashboard_read_model_commit WHERE request_id='retained-v1'",
    ).fetchone()) == before
    assert database.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0] == 2
    database.execute("BEGIN")
    assert storage.load_model(database, scope) is not None
    assert storage.authenticate_current_storage(database)


def test_two_month_async_write_queues_without_inline_model_authentication_or_collection(
    book, monkeypatch
):
    engine, save, publish, _ = book
    opening(save, publish)
    opening(save, publish, bank="bank-b", month="2026-08")
    maintenance = Maintenance(engine)
    for period in ("2026-08", "2026-09"):
        maintenance.rebuild_dashboard_read_model(period, request_id="baseline-" + period)
        _produce(engine, period)
    calls = {"authenticate": 0, "collect": 0}
    actual_authenticate = storage.authenticate_current_storage
    actual_collect = storage.collect_obsolete_blocks

    def authenticate(connection):
        calls["authenticate"] += 1
        return actual_authenticate(connection)

    def collect(connection):
        calls["collect"] += 1
        return actual_collect(connection)

    monkeypatch.setattr(storage, "authenticate_current_storage", authenticate)
    monkeypatch.setattr(storage, "collect_obsolete_blocks", collect)
    engine.register_evidence(b"later unattached evidence", "text/plain", "later",
                             request_id="two-month-write")
    assert calls == {"authenticate": 0, "collect": 0}
    with engine.store.connection(read_only=True) as connection:
        pending = dict(connection.execute(
            "SELECT period,status FROM dashboard_projection_request "
            "WHERE period IN('2026-08','2026-09')"
        ))
    assert pending == {"2026-08": "ready", "2026-09": "ready"}
    funding(save, publish, subject="later-real-funding", amount=1)
    assert calls == {"authenticate": 0, "collect": 0}
    with engine.store.connection(read_only=True) as connection:
        pending = dict(connection.execute(
            "SELECT period,status FROM dashboard_projection_request "
            "WHERE period IN('2026-08','2026-09')"
        ))
    assert pending == {"2026-08": "pending", "2026-09": "pending"}
    for period in ("2026-08", "2026-09"):
        _produce(engine, period)
        dashboard = Dashboard(engine)
        from test_dashboard_model_transactions import raw_read

        with raw_read():
            expected = dashboard.funds(period, movement_account_selection="first")
        observed = dashboard.funds(period, movement_account_selection="first")
        assert len(observed["snapshot_version"]) == 64
        observed["snapshot_version"] = expected["snapshot_version"]
        assert observed == expected
