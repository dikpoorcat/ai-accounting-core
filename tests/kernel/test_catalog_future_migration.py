"""Catalog upgrades preserve its identity and leave company versions independent."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr
from schema_fixture import TEST_FAMILY, full_contract, write_contract

from ai_accounting.kernel.catalog import Catalog, catalog_sql
from ai_accounting.kernel.contracts import Registry
from ai_accounting.kernel.migration_steps import MigrationStep
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, load_bundle
from ai_accounting.kernel.security import SecurityService
from ai_accounting.kernel.versions import contract, objects, upgrade, verify_schema


@pytest.mark.parametrize("failure_point", ["after_ddl", "before_commit"])
def test_catalog_future_upgrade_keeps_identity_history_and_company_version(tmp_path, failure_point):
    registry = Registry()
    directory = tmp_path / "contracts"
    source_sql = catalog_sql()
    added_sql = "CREATE INDEX company_name_search ON company(name)"
    target_sql = source_sql + added_sql + ";"
    source = full_contract(source_sql, kind="catalog", version=1, status="released")
    target = full_contract(target_sql, kind="catalog", version=2, status="released")
    write_contract(directory, source)
    write_contract(
        directory,
        {key: value for key, value in target.items() if key != "objects"}
        | {
            "base_version": 1,
            "base_sha256": source["sha256"],
            "add": [item for item in target["objects"] if item not in source["objects"]],
            "remove": [],
            "replace": [],
        },
    )
    write_contract(
        directory,
        full_contract(schema_sql(registry), kind="company", version=1, status="released"),
    )

    def preserve_rows(connection):
        assert list(map(tuple, connection.execute("SELECT * FROM company"))) == company_rows
        assert list(map(tuple, connection.execute("SELECT * FROM catalog_identity"))) == identity
        assert {
            table: list(map(tuple, connection.execute(f'SELECT * FROM "{table}"')))
            for table in security_rows
        } == security_rows

    step = MigrationStep(
        TEST_FAMILY,
        "catalog",
        1,
        source["sha256"],
        2,
        target["sha256"],
        lambda connection: connection.execute(added_sql),
        preserve_rows,
    )

    def bundle(version):
        return load_bundle(
            registry,
            directory,
            family=TEST_FAMILY,
            application_id=APPLICATION_ID,
            status="released",
            current_versions={"catalog": version, "company": 1},
            steps=(step,),
        )

    catalog = Catalog(tmp_path / "root", bundle(1))
    company = catalog.create_company("91310000123456789A", "合成升级公司")
    password = SecretStr("Synthetic-owner-password-2026")
    changed_password = SecretStr("Synthetic-owner-password-2027")

    def clock():
        return datetime(2026, 1, 1, tzinfo=UTC)

    source_bundle = bundle(1)
    security = SecurityService(
        catalog.path,
        clock=clock,
        catalog_validator=lambda connection: verify_schema(
            connection, bundle=source_bundle, kind="catalog"
        ),
    )
    recovery = security.provision("owner", password)
    login = security.login("owner", password)
    with catalog.connection() as connection:
        identity = list(map(tuple, connection.execute("SELECT * FROM catalog_identity")))
        company_rows = list(map(tuple, connection.execute("SELECT * FROM company")))
        security_rows = {
            table: list(map(tuple, connection.execute(f'SELECT * FROM "{table}"')))
            for table in ("security_owner", "security_session", "security_recovery")
        }
        history = list(map(tuple, connection.execute("SELECT * FROM schema_history")))
        before = objects(connection)

        def interrupt(point):
            if point == failure_point:
                raise RuntimeError("synthetic catalog migration interruption")

        with pytest.raises(RuntimeError, match="synthetic catalog"):
            upgrade(connection, bundle=bundle(2), kind="catalog", fault=interrupt)
        assert objects(connection) == before
        assert list(map(tuple, connection.execute("SELECT * FROM schema_history"))) == history
        assert verify_schema(connection, bundle=bundle(1), kind="catalog") == 1
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        preserve_rows(connection)
        assert security.authorize(login.session_token) == login.authority
        assert upgrade(connection, bundle=bundle(2), kind="catalog")
        assert objects(connection) == contract(target_sql)
        assert (
            list(map(tuple, connection.execute("SELECT * FROM schema_history WHERE version=1")))
            == history
        )
        assert [row[0] for row in connection.execute("SELECT version FROM schema_history")] == [
            1,
            2,
        ]
        preserve_rows(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable directory identity"):
            connection.execute("UPDATE catalog_identity SET instance_id='changed'")
    reopened = Catalog(tmp_path / "root", bundle(2))
    with reopened.bind(company["id"]).connection(read_only=True) as connection:
        assert verify_schema(connection, bundle=bundle(2)) == 1
        assert (
            connection.execute("SELECT database_id FROM identity").fetchone()[0]
            == company["database_id"]
        )
    target_bundle = bundle(2)
    upgraded_security = SecurityService(
        reopened.path,
        clock=clock,
        catalog_validator=lambda connection: verify_schema(
            connection, bundle=target_bundle, kind="catalog"
        ),
    )
    assert upgraded_security.authorize(login.session_token) == login.authority
    assert upgraded_security.login("owner", password).authority.owner_id == login.authority.owner_id
    reset = upgraded_security.recover("owner", recovery.recovery_code, changed_password)
    assert reset.owner_id == login.authority.owner_id
    assert upgraded_security.login("owner", changed_password).authority.owner_id == reset.owner_id
    with closing(sqlite3.connect(":memory:")) as independent:
        independent.executescript(target_sql)
        assert objects(independent) == target["objects"]
