"""The real SQLite point and leased documents stay together during concurrent work."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.storage.public import open_database_unit, verify_database

from .conftest import initial_request
from .test_rebuild_standard import repositories


def test_capture_keeps_the_old_database_and_document_bytes_after_a_real_new_commit(assembled):
    app, company = assembled
    first = initial_request(company)
    assert app.accounting_apply(first)["completion"] == "complete"
    with app.storage.read_unit(company) as read:
        metadata = app.storage.company_metadata(read)
        old_head = read.head
        old_set = app.records.document_status(read)["set_id"]
    with app.capture_backup_snapshot(company, metadata.database_id) as plan:
        capture_root = metadata.company_root / "runtime/tasks" / plan["capture_id"]
        assert plan["manifest"]["database_head"] == old_head
        assert plan["manifest"]["document_set_id"] == old_set
        second = initial_request(company, amount="70000")
        assert app.accounting_apply(second)["completion"] == "complete"
        database = capture_root / "runtime/company.sqlite3"
        assert verify_database(database).head == old_head
        with open_database_unit(
            database, repositories=repositories(), company_root=metadata.company_root
        ) as read:
            results = app.ledger.get_results(
                [
                    first["mutations"][0]["fact"]["business_id"],
                    second["mutations"][0]["fact"]["business_id"],
                ],
                read,
            )
            assert len(results) == 1
            assert results[0]["target"]["kind"] == "operations.expense"
        captured = loads_json((capture_root / "standard/current.json").read_bytes())
        current = loads_json((metadata.company_root / "standard/current.json").read_bytes())
        assert captured["set_id"] == old_set != current["set_id"]
        for item in plan["inputs"]:
            root = metadata.company_root if item["source_root"] == "company" else capture_root
            assert (root / item["relative_path"]).is_file()
        assert {
            item["path"] for item in plan["manifest"]["files"] if item["kind"] == "contract"
        } == {"contracts/company.json"}
    assert not capture_root.exists()


def test_capture_of_an_empty_company_has_complete_standard_materials_without_a_business(assembled):
    app, company = assembled
    with app.storage.read_unit(company) as read:
        metadata = app.storage.company_metadata(read)
    with app.capture_backup_snapshot(company, metadata.database_id) as plan:
        root = metadata.company_root / "runtime/tasks" / plan["capture_id"]
        manifest = loads_json((root / "standard/current.json").read_bytes())
        assert {item["key"] for item in manifest["shards"]} == {
            "objects",
            "work",
        }
        with open_database_unit(
            root / "runtime/company.sqlite3",
            repositories=repositories(),
            company_root=metadata.company_root,
        ) as read:
            assert app.ledger.latest_period(read) is None


def test_capture_rejects_the_wrong_database_before_creating_materials(assembled):
    app, company = assembled
    with app.storage.read_unit(company) as read:
        root = app.storage.company_metadata(read).company_root
    with pytest.raises(ContractError, match="旧身份"):
        with app.capture_backup_snapshot(company, str(uuid4())):
            pytest.fail("不能捕获另一数据库")
    assert not (root / "runtime/tasks").exists()
    assert not (root / "standard/current.json").exists()


def test_missing_backup_worker_is_reported_before_creating_a_capture(assembled):
    app, company = assembled
    with app.storage.read_unit(company) as read:
        database = read.database_id
    with pytest.raises(ContractError, match="未装配"):
        app.accounting_backup({"company_id": company, "expected_database_id": database})
