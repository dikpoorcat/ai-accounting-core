from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from ai_accounting import replay_cli


def test_prepare_saves_resumable_state_before_requesting_window(tmp_path, monkeypatch):
    org = uuid.uuid4()
    state_file = tmp_path / "state.json"
    system = {"companies": [{"org_id": str(org), "is_primary": True, "directory": "company"}]}
    descriptor = {
        "organization": {
            "name": "Replay window test",
            "taxpayer_identification_number": "91330106MA1234567T",
            "filing_cycle": "quarterly",
            "profile_effective_from": "2026-01-01",
            "urban_maintenance_rate": "0.07",
        }
    }
    monkeypatch.setattr(replay_cli, "verify_package", lambda _: {"manifest_sha256": "a" * 64})
    monkeypatch.setattr(
        replay_cli,
        "_load_json",
        lambda path: system if path.name == "system.json" else descriptor,
    )
    settings = SimpleNamespace(
        database_url="postgresql://test:test@localhost/catalog",
        finance_provisioning_database_url="postgresql://test:test@localhost/postgres",
        finance_migration_database_url="postgresql://test:test@localhost/postgres",
        finance_company_database_url="postgresql://test:test@localhost/finance",
    )
    monkeypatch.setattr(replay_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        replay_cli, "create_engine", lambda *args, **kwargs: SimpleNamespace(dispose=lambda: None)
    )

    class Session:
        def __init__(self, *_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def begin(self):
            return nullcontext()

        def add(self, _):
            pass

    monkeypatch.setattr(replay_cli, "Session", Session)
    monkeypatch.setattr(replay_cli, "_ensure_empty_database", lambda *args, **kwargs: True)
    monkeypatch.setattr(replay_cli.command, "upgrade", lambda *_: None)
    monkeypatch.setattr(replay_cli, "_grant_runtime_access", lambda *_: None)
    monkeypatch.setattr(replay_cli, "_initialize_empty_company", lambda **_: None)

    def fail_window(state, target):
        assert target is settings
        assert json.loads(state_file.read_text()) == state
        assert state["phase"] == "prepared"
        return {"status": "failed", "error_code": "OWNER_SECURITY_WINDOW_UNAVAILABLE"}

    monkeypatch.setattr(replay_cli, "_request_replay_security", fail_window)
    result = replay_cli.prepare_empty(tmp_path, state_file)
    assert result["status"] == "prepared"
    assert result["owner_security_window"]["status"] == "failed"
    assert json.loads(state_file.read_text())["phase"] == "prepared"
    assert "test:test" not in state_file.read_text()


@pytest.mark.parametrize("mismatch", ["none", "catalog", "registry", "physical_database"])
def test_replay_validates_catalog_registry_and_actual_company_identity(monkeypatch, mismatch):
    catalog_id, org_id, database_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    company_name = "finance_company_" + org_id.hex
    registry = SimpleNamespace(
        org_id=org_id,
        database_name=company_name,
        database_identity=database_id,
    )
    binding = SimpleNamespace(
        org_id=org_id,
        database_identity=database_id,
        current_catalog_instance_id=catalog_id,
    )
    catalog = SimpleNamespace(catalog_instance_id=catalog_id)
    state = {
        "catalog_database": "catalog",
        "catalog_instance_id": str(catalog_id),
        "primary_org_id": str(org_id),
        "companies": [
            {
                "org_id": str(org_id),
                "database_name": company_name,
                "database_identity": str(database_id),
            }
        ],
    }
    if mismatch == "catalog":
        catalog.catalog_instance_id = uuid.uuid4()
    elif mismatch == "registry":
        registry.database_identity = uuid.uuid4()
    elif mismatch == "physical_database":
        binding.current_catalog_instance_id = uuid.uuid4()
    accessed = []

    def engine(url):
        database = make_url(url).database
        accessed.append(database)
        return SimpleNamespace(database=database, dispose=lambda: None)

    class Session:
        def __init__(self, engine):
            self.engine = engine

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, model, _):
            if self.engine.database != "catalog":
                return binding
            return catalog if model is replay_cli.CatalogMetadata else registry

    monkeypatch.setattr(replay_cli, "create_engine", engine)
    monkeypatch.setattr(replay_cli, "Session", Session)
    settings = SimpleNamespace(
        multi_company_enabled=True,
        database_url="postgresql://localhost/catalog",
        finance_company_database_url="postgresql://localhost/finance",
        finance_migration_database_url=None,
    )
    if mismatch == "none":
        replay_cli._validate_replay_target(state, settings)
        assert accessed == ["catalog", company_name]
    else:
        with pytest.raises(replay_cli.ReplayError, match="REPLAY_TARGET_"):
            replay_cli._validate_replay_target(state, settings)
        if mismatch in {"catalog", "registry"}:
            assert accessed == ["catalog"]
