"""Release contracts, explicit job retry, and long-lived adapter recovery."""

from contextlib import closing

import pytest

from ai_accounting.kernel import catalog as catalog_module
from ai_accounting.kernel import daemon, schema, versions
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.security.credentials import InMemoryCredentialStore
from ai_accounting.kernel.service import default_registry


def capture_contract(monkeypatch):
    contracts = dict(versions.known_contracts("catalog"))
    objects = versions.contract(catalog_module.catalog_sql())
    contracts[catalog_module.VERSION] = {
        "version": catalog_module.VERSION,
        "objects": objects,
        "sha256": versions.fingerprint(objects).hex(),
    }
    original = versions.known_contracts
    monkeypatch.setattr(
        versions, "known_contracts", lambda kind: contracts if kind == "catalog" else original(kind)
    )


def test_catalog_forward_version_preserves_identity_and_company_version(tmp_path, monkeypatch):
    business_version = schema.VERSION
    current_version = catalog_module.VERSION
    future_version = current_version + 1
    catalog = Catalog(tmp_path, default_registry())
    with catalog.connection() as connection:
        before = tuple(connection.execute("SELECT * FROM catalog_identity").fetchone())
        before_history = [
            r[0] for r in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ]
    capture_contract(monkeypatch)
    script = (
        catalog_module.catalog_sql() + "CREATE TABLE future_setting(id INTEGER PRIMARY KEY) STRICT;"
    )
    monkeypatch.setattr(catalog_module, "VERSION", future_version)
    monkeypatch.setattr(catalog_module, "catalog_sql", lambda: script)
    with closing(connect(catalog.path)) as connection:

        def interrupted(stage):
            if stage == "before_commit":
                raise OSError("synthetic commit boundary failure")

        with pytest.raises(OSError):
            versions.upgrade(connection, kind="catalog", fault=interrupted)
        assert tuple(connection.execute("SELECT * FROM catalog_identity").fetchone()) == before
        assert (
            versions.verify_schema(connection, kind="catalog", allow_previous=True)
            == current_version
        )
        assert versions.upgrade(connection, kind="catalog")
        after = tuple(connection.execute("SELECT * FROM catalog_identity").fetchone())
        assert after == (before[0], before[1], future_version)
        assert [
            r[0] for r in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [*before_history, future_version]
    assert schema.VERSION == business_version


def test_released_ddl_change_requires_a_new_version_even_for_new_database(tmp_path, monkeypatch):
    capture_contract(monkeypatch)
    script = (
        catalog_module.catalog_sql() + "CREATE TABLE undeclared(id INTEGER PRIMARY KEY) STRICT;"
    )
    monkeypatch.setattr(catalog_module, "catalog_sql", lambda: script)
    with pytest.raises(KernelError) as error:
        Catalog(tmp_path, default_registry())
    assert error.value.code == "schema_version_bump_required"
    with closing(connect(tmp_path / "catalog.sqlite")) as connection:
        assert versions.objects(connection) == []


def test_explicit_failed_job_retry_is_bounded_audited_and_idempotent(tmp_path):
    from ai_accounting.kernel.jobs import JobRunner

    catalog = Catalog(tmp_path / "root", default_registry())
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
    original = {"catalog_id": "catalog", "port": 1}
    fresh = {"catalog_id": "catalog" if same_catalog else "different", "port": 2}
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
