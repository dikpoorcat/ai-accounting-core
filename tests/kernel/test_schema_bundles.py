"""Production and synthetic registries both require an exact family-bound contract."""

import sqlite3
from contextlib import closing

import pytest
from pydantic import Field
from schema_fixture import test_bundle

from ai_accounting.kernel.contracts import Fact, KernelError, Registry
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, FAMILY, production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import database_format, upgrade, verify_schema


def test_production_creation_records_one_draft_and_auxiliary_header(tmp_path):
    bundle = production_bundle()
    store = Store.create(tmp_path / "company.sqlite", bundle, "company", "taxpayer", "database")
    with store.connection(read_only=True) as connection:
        assert database_format(connection, bundle=bundle) == bundle.database_format("company")
        assert tuple(connection.execute("SELECT * FROM identity").fetchone()) == (
            1,
            "company",
            "taxpayer",
            "database",
        )
        assert [r[0] for r in connection.execute("SELECT version FROM schema_history")] == [0]
        assert tuple(connection.execute("SELECT * FROM schema_meta").fetchone()) == (
            1,
            FAMILY,
            "company",
            "draft",
        )
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("SELECT read_repair_revision FROM state").fetchone()[0] == 0
        assert not upgrade(connection, bundle=bundle)
    with store.path.open("rb") as handle:
        handle.seek(68)
        assert int.from_bytes(handle.read(4), "big") == APPLICATION_ID


def test_draft_creation_cannot_replace_any_existing_file(tmp_path):
    path = tmp_path / "existing.sqlite"
    path.write_bytes(b"preserve existing source")
    with pytest.raises(FileExistsError):
        Store.create(path, production_bundle(), "c", "t", "d")
    assert path.read_bytes() == b"preserve existing source"


def test_test_registry_has_real_exact_contract_and_cannot_enter_production(tmp_path):
    bundle = test_bundle(Registry())
    store = Store.create(tmp_path / "test.sqlite", bundle, "c", "t", "d")
    with store.connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=bundle) == 0
        with pytest.raises(KernelError) as error:
            verify_schema(connection, bundle=production_bundle())
        assert error.value.code == "schema_family_unsupported"
    with pytest.raises(TypeError):
        production_bundle(Registry())


def test_same_draft_number_never_upgrades_different_shape(tmp_path):
    from typing import ClassVar

    class Probe(Fact):
        kind: ClassVar[str] = "bundle_probe"
        value: int = Field(ge=0)

    first = test_bundle(Registry())
    registry = Registry()
    registry.register(Probe)
    second = test_bundle(registry)
    store = Store.create(tmp_path / "draft.sqlite", first, "c", "t", "d")
    before = store.path.read_bytes()
    with store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as error:
            upgrade(connection, bundle=second)
        assert error.value.code == "schema_fingerprint_mismatch"
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    ["application_id", "family", "kind", "status", "version", "history", "extra_history", "sql"],
)
def test_markers_history_and_exact_sql_are_independently_checked(tmp_path, damage):
    bundle = test_bundle(Registry())
    store = Store.create(tmp_path / "damaged.sqlite", bundle, "c", "t", "d")
    with closing(sqlite3.connect(store.path)) as connection:
        if damage == "application_id":
            connection.execute("PRAGMA application_id=0")
        elif damage == "version":
            connection.execute("PRAGMA user_version=1")
        elif damage in {"family", "kind", "status"}:
            original = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='immutable_schema_meta_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER immutable_schema_meta_update")
            value = {"family": "old", "kind": "catalog", "status": "released"}[damage]
            connection.execute(f"UPDATE schema_meta SET {damage}=?", (value,))
            connection.execute(original)
        elif damage == "history":
            original = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='immutable_schema_history_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER immutable_schema_history_update")
            connection.execute("UPDATE schema_history SET fingerprint=zeroblob(32)")
            connection.execute(original)
        elif damage == "extra_history":
            connection.execute(
                "INSERT INTO schema_history(version,fingerprint) VALUES(1,zeroblob(32))"
            )
        else:
            connection.execute("CREATE INDEX unexpected_index ON state(accounting)")
        connection.commit()
        with pytest.raises(KernelError):
            verify_schema(connection, bundle=bundle, allow_previous=True)


def test_unmarked_database_with_same_version_is_rejected_without_ddl(tmp_path):
    path = tmp_path / "old.sqlite"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE old_data(value TEXT)")
        connection.execute("INSERT INTO old_data VALUES('retained')")
        connection.commit()
        statements = []
        connection.set_trace_callback(statements.append)
        with pytest.raises(KernelError) as error:
            upgrade(connection, bundle=production_bundle())
        assert error.value.code == "schema_family_unsupported"
        assert not any("BEGIN IMMEDIATE" in item for item in statements)
        assert connection.execute("SELECT value FROM old_data").fetchone()[0] == "retained"
