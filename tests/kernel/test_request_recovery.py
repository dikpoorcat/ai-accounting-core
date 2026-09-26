"""Recover a lost response only from matching company-local request and audit rows."""

from __future__ import annotations

import pytest
from test_engine import engine as engine  # noqa: F401
from test_engine import evidence

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.storage import Store


def test_unknown_then_committed_and_original_idempotent_result(engine):
    assert engine.request_result("lost") == {
        "status": "unknown",
        "company_id": "company-a",
        "database_id": "db-a",
        "submitted_request_id": "lost",
    }
    source = evidence(engine)
    result = engine.save_fact(
        "test_charge", "charge", {"period": "2026-01", "amount": 100},
        evidence=(source,), expected_revision=0, request_id="lost",
    )
    assert engine.request_result("lost") == {
        "status": "committed",
        "company_id": "company-a",
        "database_id": "db-a",
        "submitted_request_id": "lost",
        "action": "confirm_fact",
        "result": result,
    }
    assert engine.save_fact(
        "test_charge", "charge", {"period": "2026-01", "amount": 100},
        evidence=(source,), expected_revision=0, request_id="lost",
    ) == result
    with pytest.raises(KernelError, match="幂等键") as failure:
        engine.save_fact(
            "test_charge", "charge", {"period": "2026-01", "amount": 101},
            evidence=(source,), expected_revision=0, request_id="lost",
        )
    assert failure.value.code == "idempotency_conflict"


def test_failed_transaction_is_unknown(engine):
    def fail(stage, connection):
        if stage == "commit":
            raise RuntimeError("synthetic failure before commit")

    broken = Engine(engine.store, fault=fail)
    with pytest.raises(RuntimeError):
        broken.register_evidence(b"test", "text/plain", "test", request_id="rolled-back")
    assert engine.request_result("rolled-back")["status"] == "unknown"


@pytest.mark.parametrize(
    "damage", ["orphan_audit", "orphan_request", "duplicate_audit", "result_mismatch"]
)
def test_request_content_damage_is_rejected(engine, damage):
    engine.register_evidence(b"test", "text/plain", "test", request_id="damaged")
    with engine.store.connection() as connection:
        table = "audit" if damage == "orphan_request" else "request"
        triggers = list(connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (table,)
        ))
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        if damage == "orphan_audit":
            connection.execute("DELETE FROM request WHERE id='damaged'")
        elif damage == "orphan_request":
            connection.execute("DELETE FROM audit WHERE request_id='damaged'")
        elif damage == "duplicate_audit":
            connection.execute(
                "INSERT INTO audit(request_id,action,payload) "
                "SELECT request_id,action,payload FROM audit WHERE request_id='damaged'"
            )
        else:
            connection.execute("UPDATE request SET result='{}' WHERE id='damaged'")
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
    with pytest.raises(KernelError) as failure:
        engine.request_result("damaged")
    assert failure.value.code == "request_content_invalid"


def test_request_identity_is_company_local(engine, tmp_path):
    engine.register_evidence(b"test", "text/plain", "test", request_id="same-id")
    other = Engine(Store.create(
        tmp_path / "other.sqlite", engine.store.bundle,
        "company-b", "91310000123456789B", "db-b",
    ))
    unknown = other.request_result("same-id")
    assert unknown["status"] == "unknown"
    assert unknown["company_id"] == "company-b"
    assert unknown["database_id"] == "db-b"
    assert engine.request_result("same-id")["status"] == "committed"
