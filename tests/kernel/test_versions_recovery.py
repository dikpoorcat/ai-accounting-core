"""Published schema contracts, forward rollback, and crash recovery boundaries."""

from contextlib import closing

import pytest

from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema import VERSION
from ai_accounting.kernel.service import LocalService, default_registry
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import baseline, objects, upgrade, verify_schema


def old_file(path, kind):
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for typ in ("table", "index", "trigger"):
            for item in baseline(kind)["objects"]:
                if item["type"] == typ:
                    connection.execute(item["sql"])
        if kind == "business":
            connection.execute(
                "INSERT INTO identity VALUES(1,'company','91310000123456789A','database',1)"
            )
            connection.execute("INSERT INTO state VALUES(1,0,0,0,1)")
            connection.execute("PRAGMA user_version=1")
        connection.commit()


def test_known_committed_schema_upgrades_and_partial_ddl_rolls_back(tmp_path):
    path = tmp_path / "company.sqlite"
    old_file(path, "business")
    registry = default_registry()
    with closing(connect(path)) as connection:
        before = objects(connection)

        def fail(stage):
            if stage == "before_commit":
                raise RuntimeError("interrupted")

        with pytest.raises(RuntimeError):
            upgrade(connection, registry=registry, fault=fail)
        assert objects(connection) == before
        assert connection.execute("SELECT schema_version FROM identity").fetchone()[0] == 1
        assert upgrade(connection, registry=registry)
        assert verify_schema(connection, registry=registry) == VERSION
        assert not upgrade(connection, registry=registry)
        assert [
            r[0] for r in connection.execute("SELECT version FROM schema_history ORDER BY version")
        ] == [1, VERSION]


@pytest.mark.parametrize(
    "mutation",
    ["DROP TRIGGER seal_voucher", "DROP TABLE monthly_cashflow", "PRAGMA user_version=99"],
)
def test_advertised_version_never_bypasses_schema_check(tmp_path, mutation):
    store = Store.create(
        tmp_path / "company.sqlite", default_registry(), "company", "taxpayer", "database"
    )
    with store.connection(read_only=True):
        pass
    with closing(connect(store.path)) as connection:
        connection.execute(mutation)
    with pytest.raises(KernelError):
        with store.connection():
            pytest.fail("an unsupported database was opened for writing")


def test_catalog_known_v1_upgrades_but_unknown_nonempty_catalog_is_rejected(tmp_path):
    old_file(tmp_path / "catalog.sqlite", "catalog")
    catalog = Catalog(tmp_path, default_registry())
    assert catalog.companies() == []
    with catalog.connection() as connection:
        connection.execute("DROP TRIGGER immutable_catalog_identity_update")
    with pytest.raises(KernelError):
        Catalog(tmp_path, default_registry())


@pytest.mark.parametrize(
    "stage",
    [
        "operation_recorded",
        "file_prepared",
        "file_published",
        "before_registration_commit",
        "registration_committed",
    ],
)
def test_company_creation_recovers_same_file_and_identity(tmp_path, stage):
    class Terminated(BaseException):
        pass

    def crash(point):
        if point == stage:
            raise Terminated()

    catalog = Catalog(tmp_path, default_registry(), fault=crash)
    with pytest.raises(Terminated):
        catalog.create_company("91310000123456789A", "中断恢复测试")
    recovered = Catalog(tmp_path, default_registry())
    companies = recovered.companies()
    assert len(companies) == 1
    assert recovered.create_company("91310000123456789A", "中断恢复测试") == companies[0]
    assert recovered.operations()[0]["status"] == "succeeded"
    with recovered.bind(companies[0]["id"]).connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM identity").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("command", "payload"),
    [
        ("companies", {"unexpected": True}),
        ("companies", []),
        ("create_company", {"taxpayer_id": 123, "name": "company"}),
        ("run_jobs", {"company_id": "company", "limit": True}),
        ("jobs", {"company_id": "company", "limit": 2.5}),
    ],
)
def test_every_public_command_validates_before_company_binding(tmp_path, command, payload):
    service = LocalService(tmp_path)
    with pytest.raises(KernelError) as error:
        service.dispatch(command, payload)
    assert error.value.code == "invalid_command"
    assert service.catalog.companies() == []
