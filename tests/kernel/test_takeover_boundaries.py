"""Release contracts, explicit job retry, and long-lived adapter recovery."""

import pytest

from ai_accounting.kernel import daemon
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.security.credentials import InMemoryCredentialStore


def test_explicit_failed_job_retry_is_bounded_audited_and_idempotent(tmp_path):
    from ai_accounting.kernel.jobs import JobRunner

    catalog = Catalog(tmp_path / "root")
    company = catalog.create_company("91310000123456789A", "合成重试公司")
    engine = Engine(catalog.bind(company["id"]))
    blocker = tmp_path / "blocked-directory"
    blocker.write_text("synthetic filesystem blocker")
    queued = engine.queue_backup(str(blocker), request_id="backup")
    for _ in range(4):
        JobRunner(catalog).run_once()
    failed = engine.jobs()[0]
    assert failed["status"] == "failed" and failed["attempts"] == 3
    retry = engine.retry_job(queued["job_id"], request_id="retry")
    assert retry["previous_attempts"] == 3 and retry["previous_error"]
    assert engine.retry_job(queued["job_id"], request_id="retry") == retry
    assert engine.jobs()[0]["attempts"] == 0
    with pytest.raises(KernelError) as error:
        engine.retry_job(queued["job_id"], request_id="different-retry")
    assert error.value.code == "job_not_failed"
    blocker.unlink()
    JobRunner(catalog).run_once()
    assert engine.jobs()[0]["status"] == "succeeded"
    assert engine.retry_job(queued["job_id"], request_id="retry") == retry


@pytest.mark.parametrize("same_catalog", [True, False])
def test_adapter_reconnect_preserves_request_only_for_same_catalog(
    tmp_path, monkeypatch, same_catalog
):
    catalog = Catalog(tmp_path)
    with catalog.connection() as connection:
        catalog_id = connection.execute(
            "SELECT instance_id FROM catalog_identity WHERE id=1"
        ).fetchone()[0]
    original = {
        "protocol": daemon.SERVICE_PROTOCOL,
        "catalog_id": catalog_id,
        "database_format": catalog.database_format(),
        "pid": 12345,
        "port": 1,
        "capability": "synthetic-service-capability",
        "build_id": "synthetic-build",
    }
    fresh = {
        **original,
        "catalog_id": original["catalog_id"] if same_catalog else "different",
        "port": 2,
    }
    seen = []

    def request(metadata, path, payload, **kwargs):
        seen.append((metadata, path, payload))
        if metadata["port"] == 1:
            raise OSError("synthetic resident restart")
        return {"status": "saved"}

    monkeypatch.setattr(daemon, "_request", request)
    monkeypatch.setattr(daemon, "ensure_service", lambda root: fresh)
    client = daemon.ServiceClient(
        tmp_path, metadata=original, credential_store=InMemoryCredentialStore()
    )
    payload = {"company_id": "company", "request_id": "same-idempotency-key"}
    if same_catalog:
        assert client.dispatch("backup", payload) == {"status": "saved"}
        assert seen[0][2] == seen[1][2] == {"command": "backup", "payload": payload}
    else:
        with pytest.raises(KernelError) as error:
            client.dispatch("backup", payload)
        assert error.value.code == "service_unavailable"
        assert len(seen) == 1
