"""The installed draft accepts its new baseline and preserves rejected archived files."""

from contextlib import closing

import pytest
from archived_draft_fixture import FINGERPRINTS, archived_company

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.backup import verify_file
from ai_accounting.kernel.contract_files import production_contract_file_names
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import objects, verify_schema


def test_production_bundle_has_no_archived_draft_sources_or_transitions():
    bundle = production_bundle()
    assert bundle.status == "draft"
    assert dict(bundle.development_contracts) == {}
    assert dict(bundle.draft_transitions) == {}
    assert set(production_contract_file_names()) == {"company/draft.json", "catalog/draft.json"}


def test_new_baseline_has_only_its_actual_installation_history(tmp_path):
    bundle = production_bundle()
    store = Store.create(
        tmp_path / "company.sqlite",
        bundle,
        "synthetic-company",
        "913100000000000001",
        "synthetic-database",
    )
    with store.connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=bundle) == 0
        assert [
            tuple(row)
            for row in connection.execute("SELECT version,hex(fingerprint) FROM schema_history")
        ] == [(0, bundle.current()["sha256"].upper())]
        assert connection.execute("SELECT count(*) FROM schema_draft_history").fetchone()[0] == 0
        expected_index = next(
            item for item in bundle.current()["objects"]
            if item["name"] == "close_reference_direct_adoption"
        )
        assert expected_index in objects(connection)
        for table in (
            "dashboard_read_model_commit", "dashboard_read_model_block",
            "dashboard_read_model_current",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert verify_file(store.path, _bundle=bundle)["verification"]["status"] == "verified"


@pytest.mark.parametrize("fingerprint", FINGERPRINTS)
def test_archived_draft_is_rejected_without_writing_or_fabricating_migration(tmp_path, fingerprint):
    path, source = archived_company(tmp_path / "archived.sqlite", fingerprint)
    original_file = path.read_bytes()
    bundle = production_bundle()
    with closing(connect(path)) as connection:
        before = tuple(connection.iterdump())
        with pytest.raises(KernelError) as mismatch:
            verify_schema(connection, bundle=bundle)
        assert mismatch.value.code == "schema_fingerprint_mismatch"
        with pytest.raises(KernelError) as undeclared:
            development.upgrade_company(connection, bundle)
        assert undeclared.value.code == "migration_not_declared"
        assert not connection.in_transaction
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert tuple(connection.iterdump()) == before
        assert verify_schema(connection, bundle=source) == 0
    assert path.read_bytes() == original_file


def test_production_draft_upgrade_rejects_before_creating_a_root(tmp_path):
    root = tmp_path / "nonexistent-root"
    with pytest.raises(KernelError) as failure:
        development.upgrade_root(root, bundle=production_bundle())
    assert failure.value.code == "migration_not_declared"
    assert not root.exists()
