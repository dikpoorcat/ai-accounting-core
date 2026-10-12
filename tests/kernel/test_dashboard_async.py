"""Synthetic source-bound publication and recovery, independent of real books."""

import hashlib
import sqlite3
from contextlib import contextmanager

import pytest
from test_replay_close_range import replay_book as _replay_book

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_async import (
    _fingerprint,
    _produce,
    _store_path,
    delivery_state,
    run_pending,
)

replay_book = _replay_book


def test_business_commit_never_builds_dashboard_and_worker_publishes(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM dashboard_read_model_commit").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM dashboard_projection_publication").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM dashboard_projection_request").fetchone()[0]
            == 3
        )
    assert delivery_state(engine, "2026-01")["state"] == "preparing"
    outcome = _produce(engine, "2026-01")
    assert outcome["status"] == "ready"
    assert delivery_state(engine, "2026-01")["state"] == "ready"
    response = Dashboard(engine).brief("2026-01")
    assert response["snapshot_version"] == outcome["model_version"]
    assert response["data"] is not None
    assert _store_path(engine).is_file()


def test_unrelated_evidence_does_not_rebuild_and_corruption_is_failed(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    outcome = _produce(engine, "2026-01")
    engine.register_evidence(
        b"unrelated original", "text/plain", "unrelated", request_id="unrelated"
    )
    state = delivery_state(engine, "2026-01")
    if state["state"] == "preparing":
        assert run_pending(engine, limit=1)[0]["status"] == "reused"
    assert delivery_state(engine, "2026-01")["model_version"] == outcome["model_version"]
    with sqlite3.connect(_store_path(engine)) as connection:
        connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")
    assert delivery_state(engine, "2026-01") == {
        "state": "failed",
        "model_version": None,
        "reason": "corrupt",
    }


def test_empty_selector_fingerprint_changes_on_first_matching_source():
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE source(period INTEGER, amount INTEGER)")
        sql = "SELECT amount FROM source WHERE period<=12 ORDER BY period"
        original = _fingerprint(connection, sql)
        connection.execute("INSERT INTO source VALUES(49,100)")
        assert _fingerprint(connection, sql) == original
        connection.execute("INSERT INTO source VALUES(12,50)")
        assert _fingerprint(connection, sql) != original


def test_http_preparing_has_no_money_and_ready_uses_typed_content(replay_book):
    result = replay_book.call("dashboard_brief", period="2026-01")
    assert result["state"] == "preparing"
    assert "response" not in result
    engine = replay_book.app.engine(replay_book.company)
    _produce(engine, "2026-01")
    result = replay_book.call("dashboard_brief", period="2026-01")
    assert result["state"] == "ready"
    assert result["model_version"] == result["response"]["snapshot_version"]


def test_source_change_before_publication_discards_then_recovers(replay_book):
    engine = replay_book.app.engine(replay_book.company)

    def concurrent_change(stage, connection):
        if stage == "dashboard_before_publish":
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?)",
                (hashlib.sha256(b"later source").digest(), b"later source", "text/plain", "later"),
            )

    engine.fault = concurrent_change
    assert _produce(engine, "2026-01")["status"] == "superseded"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM dashboard_projection_publication").fetchone()[
                0
            ]
            == 0
        )
    engine.fault = lambda stage, connection: None
    assert _produce(engine, "2026-01")["status"] == "ready"


def test_authorization_gate_only_wraps_commit_and_revocation_rolls_back(replay_book):
    engine = replay_book.app.engine(replay_book.company)
    gate = {"active": False, "revoked": False}

    @contextmanager
    def authority():
        assert not gate["active"]
        gate["active"] = True
        try:
            if gate["revoked"]:
                raise KernelError("permission_revoked", "synthetic authority revoked")
            yield
        finally:
            gate["active"] = False

    engine.commit_guard = authority

    def revoke(stage, connection):
        assert not gate["active"]
        if stage == "commit":
            gate["revoked"] = True

    engine.fault = revoke
    with pytest.raises(KernelError) as error:
        engine.register_evidence(b"must roll back", "text/plain", "revoked", request_id="revoked")
    assert error.value.code == "permission_revoked"
    assert engine.request_result("revoked")["status"] == "unknown"
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM evidence WHERE digest=?",
                (hashlib.sha256(b"must roll back").digest(),),
            ).fetchone()
            is None
        )


def test_explicit_projection_repair_restarts_derived_generation(replay_book):
    from ai_accounting.kernel.maintenance import Maintenance

    engine = replay_book.app.engine(replay_book.company)
    _produce(engine, "2026-01")
    original = _store_path(engine)
    with sqlite3.connect(original) as connection:
        connection.execute("UPDATE dashboard_read_model_block SET payload='{}'")
    result = Maintenance(engine).rebuild_dashboard_read_model("2026-01", request_id="repair")
    assert result["status"] == "pending" and result["changed"] is True
    assert _store_path(engine) != original and original.exists()
    assert run_pending(engine, limit=1)[0]["status"] == "ready"
    assert delivery_state(engine, "2026-01")["state"] == "ready"
