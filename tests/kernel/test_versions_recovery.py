"""Current contract rejection and durable company-creation recovery."""

from contextlib import closing

import pytest

from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.service import LocalService
from ai_accounting.kernel.storage import Store


@pytest.mark.parametrize(
    "mutation",
    ["DROP TRIGGER seal_voucher", "DROP TABLE monthly_cashflow", "PRAGMA user_version=99"],
)
def test_advertised_version_never_bypasses_schema_check(tmp_path, mutation):
    store = Store.create(
        tmp_path / "company.sqlite", production_bundle(), "company", "taxpayer", "database"
    )
    with store.connection(read_only=True):
        pass
    with closing(connect(store.path)) as connection:
        connection.execute(mutation)
    with pytest.raises(KernelError):
        with store.connection():
            pytest.fail("an unsupported database was opened for writing")


def test_current_catalog_reopens_but_missing_identity_guard_is_rejected(tmp_path):
    catalog = Catalog(tmp_path, production_bundle())
    assert catalog.companies() == []
    with catalog.connection() as connection:
        connection.execute("DROP TRIGGER immutable_catalog_identity_update")
    with pytest.raises(KernelError):
        Catalog(tmp_path, production_bundle())


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

    catalog = Catalog(tmp_path, production_bundle(), fault=crash)
    with pytest.raises(Terminated):
        catalog.create_company("91310000123456789A", "中断恢复测试")
    recovered = Catalog(tmp_path, production_bundle())
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
