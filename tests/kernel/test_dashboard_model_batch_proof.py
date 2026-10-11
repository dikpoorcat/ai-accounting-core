"""Batch reuse preserves every receipt, original byte and audit-member proof."""

import copy
from dataclasses import replace

import pytest
from test_dashboard_model_retention import verifier
from test_dashboard_read_model_storage import anchor, assert_corrupt
from test_dashboard_read_model_storage import database as database  # noqa: F401
from test_dashboard_read_model_storage import native as native  # noqa: F401

from ai_accounting.kernel import dashboard_read_model as storage
from ai_accounting.kernel.dashboard_model_integrity import verify_models
from ai_accounting.kernel.types import canonical, digest


def batch(database, native):
    scope, blocks = native
    models = []
    for period in ("2026-08", "2026-09"):
        scoped = scope.model_copy(update={"period": period})
        payload = copy.deepcopy(blocks[0].payload)
        payload["selected_period"].update({
            "key": period, "year": 2026, "month": int(period[-2:]),
            "label": f"2026 年 {int(period[-2:])} 月", "short_label": f"{int(period[-2:])} 月",
            "start_date": period + "-01", "end_date": period + "-31"
            if period.endswith("08") else period + "-30",
        })
        models.append(storage.prepare_model(
            scoped, [replace(blocks[0], payload=payload)], request_id="synthetic-two-month-batch",
        ))
    with database:
        audit_id = anchor(database, models)
        for model in models:
            storage.persist_model(database, model, audit_id=audit_id, collect_obsolete=False)
    return scope, models


def consume(database, scope, consumer):
    if consumer == "current":
        return storage.authenticate_current_storage(database)
    return verify_models(verifier(scope), database, reconstruct_current=False)


@pytest.mark.parametrize("consumer", ["current", "complete"])
def test_batch_audit_and_request_are_checked_once_per_consumer_call(
    database, native, monkeypatch, consumer,
):
    scope, _ = batch(database, native)
    counts = {"audit": 0, "request": 0}
    original_audit, original_request = storage._audit_descriptors, storage._check_request

    def audit(raw):
        counts["audit"] += 1
        return original_audit(raw)

    def request(*args):
        counts["request"] += 1
        return original_request(*args)

    monkeypatch.setattr(storage, "_audit_descriptors", audit)
    monkeypatch.setattr(storage, "_check_request", request)
    for expected in (1, 2):
        database.execute("BEGIN")
        assert consume(database, scope, consumer)
        assert counts == {"audit": expected, "request": expected}
        database.rollback()


def test_unowned_autocommit_reader_does_not_reuse_batch_proofs(database, native, monkeypatch):
    scope, _ = batch(database, native)
    original = storage._audit_descriptors
    calls = []

    def audit(raw):
        calls.append(raw)
        return original(raw)

    monkeypatch.setattr(storage, "_audit_descriptors", audit)
    assert not database.in_transaction
    assert verify_models(verifier(scope), database, compare_content=False)["receipts"] == 2
    assert len(calls) == 3  # Two commit proofs and the independent complete audit proof.


@pytest.mark.parametrize("field", ["anchor_payload", "request_result", "request_digest"])
def test_same_batch_id_cannot_reuse_different_original_bytes(database, native, field):
    batch(database, native)
    database.execute("BEGIN")
    rows = [dict(row) for row in database.execute(
        "SELECT c.*,a.request_id anchor_request_id,a.action anchor_action,"
        "a.payload anchor_payload,r.result request_result,r.digest request_digest "
        "FROM dashboard_read_model_commit c JOIN audit a ON a.id=c.audit_id "
        "JOIN request r ON r.id=c.request_id ORDER BY c.period",
    )]
    cache = {}
    storage._anchored_descriptor(rows[0], _verified_batches=cache)
    altered = {**rows[1], field: bytes(32) if field == "request_digest" else rows[1][field] + " "}
    assert_corrupt(lambda: storage._anchored_descriptor(altered, _verified_batches=cache))
    # Rejection must not replace the proof or poison a subsequent exact member.
    assert storage._anchored_descriptor(rows[1], _verified_batches=cache).scope.period == "2026-09"


@pytest.mark.parametrize("field", ["root_digest", "parent_id", "id"])
def test_a_verified_batch_does_not_authenticate_a_damaged_commit_row(database, native, field):
    batch(database, native)
    database.execute("BEGIN")
    ids = [row[0] for row in database.execute(
        "SELECT id FROM dashboard_read_model_commit ORDER BY period",
    )]
    cache = {}
    storage._anchored_descriptor(storage._receipt_row(database, ids[0]), _verified_batches=cache)
    row = dict(storage._receipt_row(database, ids[1]))
    row[field] = bytes(32)
    assert_corrupt(lambda: storage._anchored_descriptor(row, _verified_batches=cache))


@pytest.mark.parametrize("consumer", ["current", "complete"])
@pytest.mark.parametrize("damage", ["missing_member", "damaged_member"])
def test_batch_member_damage_is_rejected_even_with_rehashed_original_request(
    database, native, consumer, damage,
):
    scope, _ = batch(database, native)
    payload = storage.loads_unique(database.execute("SELECT payload FROM audit").fetchone()[0])
    if damage == "missing_member":
        payload["dashboard_models"].pop()
    else:
        payload["dashboard_models"][-1]["root_digest"] = "0" * 64
    with database:
        database.execute("UPDATE audit SET payload=?", (canonical(payload),))
        database.execute("UPDATE request SET result=?,digest=?",
                         (canonical(payload), digest(["dashboard_read_model", payload])))
    database.execute("BEGIN")
    assert_corrupt(lambda: consume(database, scope, consumer))


def test_complete_batch_still_rejects_audit_member_without_a_committed_receipt(database, native):
    scope, models = batch(database, native)
    with database:
        database.execute("DROP TRIGGER immutable_dashboard_read_model_commit_delete")
        database.execute("DELETE FROM dashboard_read_model_current WHERE period='2026-09'")
        database.execute("DELETE FROM dashboard_read_model_commit WHERE id=?",
                         (bytes.fromhex(models[-1].descriptor["commit_id"]),))
    database.execute("BEGIN")
    assert_corrupt(lambda: verify_models(verifier(scope), database, reconstruct_current=False))
