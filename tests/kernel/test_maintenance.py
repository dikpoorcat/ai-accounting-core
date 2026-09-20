"""Exercise repairs through their transaction and public command boundaries."""

from functools import partial

import pytest
from pydantic import SecretStr
from test_engine import close, publish, save
from test_engine import engine as engine  # noqa: F401
from test_integrity_content import damage, frozen_opening, opening_engine

from ai_accounting.kernel.backup import BackupError, create_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.diagnostics import error_response
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.permissions import PrivatePathError
from ai_accounting.kernel.service import LocalService


def test_private_file_error_is_explicit_without_exposing_local_path(tmp_path):
    secret_path = tmp_path / "private-company-name.sqlite"
    result = error_response(PrivatePathError(secret_path))
    assert result["code"] == "private_path_permission_required"
    assert str(secret_path) not in str(result)


def frozen_state(engine):
    with engine.store.connection(read_only=True) as connection:
        return list(connection.iterdump())


@pytest.mark.parametrize(
    "target,stage",
    [
        ("projections", "repair_verified"),
        ("projections", "projection_cleared"),
        ("projections", "projection_rebuilt"),
        ("projections", "repair_applied"),
        ("read_indexes", "after_unseal"),
        ("read_indexes", "after_rebuild"),
        ("read_indexes", "before_restore"),
        ("read_indexes", "after_verify"),
    ],
)
def test_repair_failure_rolls_back_data_ddl_counter_request_and_audit(engine, target, stage):
    save(engine)
    publish(engine)
    close(engine)
    if target == "projections":
        with engine.store.connection() as connection:
            connection.execute("UPDATE monthly_account SET debit=debit+1")
    else:
        damage(
            engine, "read_index_source", "UPDATE read_index_source SET source_digest=zeroblob(32)"
        )
    before = frozen_state(engine)

    def fail(point, _connection):
        if point == stage:
            raise RuntimeError("synthetic interrupted repair")

    failed = Maintenance(Engine(engine.store, fault=fail))
    method = failed.rebuild_projections if target == "projections" else failed.repair_read_indexes
    with pytest.raises(RuntimeError, match="synthetic interrupted repair"):
        method(request_id="repair")
    assert frozen_state(engine) == before
    repaired = Maintenance(engine)
    retry = (
        repaired.rebuild_projections if target == "projections" else repaired.repair_read_indexes
    )
    assert retry(request_id="repair")["changed"]
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT read_repair_revision FROM state").fetchone()[0] == 1
    assert Maintenance(engine).verify_integrity()["status"] == "verified"


def test_index_repair_is_idempotent_and_does_not_touch_business_epochs(engine):
    save(engine)
    publish(engine)
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        epochs = engine.store.epochs(connection)
    damage(engine, "close_reference", "DELETE FROM close_reference")
    maintenance = Maintenance(engine)
    result = maintenance.repair_read_indexes(request_id="repair")
    assert result["changed"] and result["read_repair_revision"] == 1
    assert maintenance.repair_read_indexes(request_id="repair") == result
    assert not maintenance.repair_read_indexes(request_id="no-op")["changed"]
    with engine.store.connection(read_only=True) as connection:
        assert engine.store.epochs(connection) == epochs
        assert connection.execute("SELECT read_repair_revision FROM state").fetchone()[0] == 1


@pytest.mark.parametrize("target", ["projections", "read_indexes"])
def test_source_damage_cannot_be_repaired_or_disguised(engine, target):
    save(engine)
    publish(engine)
    damage(engine, "voucher_line", "UPDATE voucher_line SET account='5601' WHERE debit>0")
    before = frozen_state(engine)
    maintenance = Maintenance(engine)
    method = (
        maintenance.rebuild_projections
        if target == "projections"
        else maintenance.repair_read_indexes
    )
    with pytest.raises(KernelError) as failure:
        method(request_id="repair")
    assert failure.value.code == "content_integrity_failed"
    assert frozen_state(engine) == before


def test_missing_direct_opening_adoption_blocks_backup_without_mutating_source(tmp_path):
    engine = opening_engine(tmp_path)
    frozen_opening(engine, selected=False)
    before = frozen_state(engine)
    with pytest.raises(BackupError) as failure:
        create_portable(engine.store.path, tmp_path / "backups", _bundle=engine.store.bundle)
    assert failure.value.code == "backup_content_invalid"
    assert "content_integrity_failed" in str(failure.value)
    assert frozen_state(engine) == before


def test_new_commands_are_available_through_the_existing_service_and_schema(tmp_path):
    service = LocalService(tmp_path / "service")
    password = SecretStr("Synthetic-maintenance-owner-123")
    service.security.provision("owner", password)
    token = service.security.login("owner", password).session_token
    dispatch = partial(service.dispatch, session_token=token)
    company = dispatch("create_company", {"taxpayer_id": "91310000123456789A", "name": "合成公司"})
    assert "id" in company, company
    response = dispatch("verify_integrity", {"company_id": company["id"]})
    assert response["status"] == "verified"
    result = dispatch("repair_read_indexes", {"company_id": company["id"], "request_id": "repair"})
    assert result["status"] == "repaired" and result["changed"] is False
    schema = dispatch("schema", {})
    assert "verify_integrity" in schema["commands"]
    assert "repair_read_indexes" in schema["commands"]
