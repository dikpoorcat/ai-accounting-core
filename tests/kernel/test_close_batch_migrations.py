"""Released company v3 / catalogue v2 advance independently and atomically."""

from contextlib import closing

import pytest
from test_identity import PASSWORD

from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.security import SecurityService
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.versions import (
    current_version,
    known_contracts,
    objects,
    upgrade,
    verify_schema,
)


def previous_file(path, kind, version):
    snapshot = known_contracts(kind)[version]
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for typ in ("table", "index", "view", "trigger"):
            for item in snapshot["objects"]:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        if kind == "business":
            connection.execute(
                "INSERT INTO identity VALUES(1,'synthetic-company',"
                "'synthetic-taxpayer','synthetic-db',?)",
                (version,),
            )
            connection.execute("INSERT INTO state VALUES(1,0,0,0,1)")
            connection.execute(
                "INSERT INTO evidence VALUES(?,?,?,?)",
                (b"a" * 32, b"synthetic retained evidence", "text/plain", "retained"),
            )
        else:
            connection.execute(
                "INSERT INTO catalog_identity VALUES(1,'synthetic-catalog',?)", (version,)
            )
        connection.execute(
            "INSERT INTO schema_history(version,fingerprint) VALUES(?,?)",
            (version, bytes.fromhex(snapshot["sha256"])),
        )
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()


@pytest.mark.parametrize(
    ("kind", "previous", "table"),
    [("business", 3, "security_close_batch_receipt"), ("catalog", 2, "security_close_batch")],
)
@pytest.mark.parametrize("stage", ["after_ddl", "before_commit"])
def test_new_security_schema_forward_upgrade_and_fault_rollback(
    tmp_path, kind, previous, table, stage
):
    path = tmp_path / (kind + ".sqlite")
    previous_file(path, kind, previous)
    registry = default_registry()
    with closing(connect(path)) as connection:
        before = objects(connection)
        assert (
            verify_schema(connection, kind=kind, registry=registry, allow_previous=True) == previous
        )

        def fail(point):
            if point == stage:
                raise RuntimeError("synthetic migration interruption")

        with pytest.raises(RuntimeError):
            upgrade(connection, kind=kind, registry=registry, fault=fail)
        assert objects(connection) == before
        assert (
            verify_schema(connection, kind=kind, registry=registry, allow_previous=True) == previous
        )
        assert upgrade(connection, kind=kind, registry=registry)
        assert verify_schema(connection, kind=kind, registry=registry) == current_version(kind)
        assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert [
            row[0]
            for row in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [previous, current_version(kind)]
        if kind == "business":
            assert (
                connection.execute("SELECT content FROM evidence").fetchone()[0]
                == b"synthetic retained evidence"
            )
        assert not upgrade(connection, kind=kind, registry=registry)


def test_catalogue_upgrade_preserves_owner_session_and_recovery(tmp_path):
    path = tmp_path / "catalog.sqlite"
    previous_file(path, "catalog", 2)
    old = SecurityService(
        path,
        catalog_validator=lambda connection: verify_schema(
            connection, kind="catalog", allow_previous=True
        ),
    )
    recovery = old.provision("owner", PASSWORD)
    login = old.login("owner", PASSWORD)
    with closing(connect(path)) as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("security_owner", "security_session", "security_recovery")
        }
        assert upgrade(connection, kind="catalog")
        after = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in before
        }
        assert after == before
    current = SecurityService(path)
    assert current.authorize(login.session_token) == login.authority
    assert recovery.recovery_code.get_secret_value()
