from __future__ import annotations

import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    RepositoryFactory,
    Storage,
    company_structure_contract,
    create_catalog,
    module_schema,
    restore_database_copy,
    verify_database,
)


def test_structure_contracts_assign_triggers_to_their_table_owner():
    ledger = module_schema("ledger")
    files = module_schema("files")
    assert "voucher_frozen_update" in {item.name for item in ledger.objects}
    assert files.tables == frozenset({"file_task"})
    assert not any(item.type == "trigger" for item in files.objects)


def test_new_company_contains_one_complete_module_baseline(storage_case):
    _, company = storage_case
    owners = sorted({item["owner"] for item in company_structure_contract()["objects"]})
    with sqlite3.connect(company.database_path) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(module_schema)")] == [
            "module_key", "version", "contract_digest", "sql_fingerprint"
        ]
        assert connection.execute(
            "SELECT module_key,version FROM module_schema ORDER BY module_key"
        ).fetchall() == [(owner, 1) for owner in owners]
    assert verify_database(company.database_path).head == 0


@pytest.mark.parametrize(
    "change",
    [
        "DROP INDEX identity_object_kind_sort",
        "UPDATE module_schema SET contract_digest='" + "f" * 64 + "' WHERE module_key='ledger'",
        "UPDATE system_meta SET structure_fingerprint='" + "f" * 64 + "'",
        "CREATE TABLE surprise(value TEXT) STRICT",
        "ALTER TABLE module_schema ADD COLUMN parent_digest TEXT",
        "UPDATE module_schema SET version=2 WHERE module_key='files'",
        "DELETE FROM module_schema WHERE module_key='records'",
        "UPDATE module_schema SET sql_fingerprint='" + "f" * 64 + "' WHERE module_key='storage'",
    ],
)
def test_changed_structure_or_record_is_rejected(storage_case, change):
    storage, company = storage_case
    connection = sqlite3.connect(company.database_path)
    try:
        connection.execute(change)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ContractError) as error:
        verify_database(company.database_path)
    assert error.value.code == "storage_corrupt"


def test_unknown_or_corrupt_database_rejected_without_modification(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE old_data(value TEXT)")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(ContractError):
        verify_database(path)
    assert path.read_bytes() == before
    path.write_bytes(b"not SQLite")
    with pytest.raises(ContractError):
        verify_database(path)
    assert path.read_bytes() == b"not SQLite"


def test_new_targets_never_replace_existing_files(storage_case, tmp_path):
    storage, company = storage_case
    before = storage.catalog_path.read_bytes()
    with pytest.raises(ContractError) as error:
        create_catalog(storage.catalog_path, OwnerProfile(str(uuid4()), "另一老板", "hash"))
    assert error.value.code == "target_exists"
    assert storage.catalog_path.read_bytes() == before
    root = tmp_path / "blocked"
    path = root / "runtime/company.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"retain me")
    with pytest.raises(ContractError) as error:
        storage.create_company(
            CompanySpec(str(uuid4()), str(uuid4()), "933333333333333333", "新公司", "2026-01", root)
        )
    assert error.value.code == "target_exists" and path.read_bytes() == b"retain me"
    assert [item.company_id for item in storage.list_companies()] == [company.company_id]


def test_repository_contract_must_match_and_owner_is_unique(storage_case):
    storage, _ = storage_case
    schema = module_schema("identity")
    factory = RepositoryFactory(schema, lambda session: session)
    with pytest.raises(ContractError):
        Storage(storage.catalog_path, repositories=[factory, factory])
    with pytest.raises(ContractError):
        Storage(
            storage.catalog_path, repositories=[replace(factory, schema=replace(schema, version=2))]
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"credit_code": "91111111111111111a"},
        {"start_period": "2026-13"},
        {"company_id": "not-a-company-id"},
    ],
)
def test_invalid_company_identity_never_creates_a_database(storage_case, tmp_path, changes):
    storage, _ = storage_case
    root = tmp_path / "invalid-company"
    spec = CompanySpec(
        str(uuid4()), str(uuid4()), "944444444444444444", "合成公司", "2026-01", root
    )
    with pytest.raises(ContractError) as error:
        storage.create_company(replace(spec, **changes))
    assert error.value.code == "invalid_input"
    assert not root.exists()


def test_snapshot_is_bound_to_read_head_then_restores_without_registration(storage_case, tmp_path):
    storage, company = storage_case
    second = Storage(storage.catalog_path, repositories=storage._factories.values())
    with storage.read_unit(company.company_id) as before:
        with second.write_unit(company.company_id) as writer:
            writer.repository("identity").add(name="快照之后")
        captured = storage.capture_database(before, tmp_path / "snapshot.sqlite3")
        assert captured.head == 0
    assert verify_database(company.database_path).head == 1
    restored = restore_database_copy(captured.path, tmp_path / "restored.sqlite3")
    assert restored.head == 0 and restored.database_id == company.database_id
    assert [item.company_id for item in storage.list_companies()] == [company.company_id]
    with pytest.raises(ContractError) as error:
        restore_database_copy(captured.path, restored.path)
    assert error.value.code == "target_exists"


def test_snapshot_refuses_active_write_unit(storage_case, tmp_path):
    storage, company = storage_case
    with storage.write_unit(company.company_id) as unit:
        with pytest.raises(ContractError, match="只读一致点"):
            storage.capture_database(unit, tmp_path / "not-a-snapshot.sqlite3")
    assert not (tmp_path / "not-a-snapshot.sqlite3").exists()


def test_company_list_does_not_open_or_require_business_database(storage_case):
    storage, company = storage_case
    company.database_path.write_bytes(b"broken company database")
    locations = storage.list_companies()
    assert len(locations) == 1 and locations[0].company_id == company.company_id
    with pytest.raises(ContractError):
        with storage.read_unit(company.company_id):
            pass


def test_restored_location_requires_explicit_registration(storage_case, tmp_path):
    storage, company = storage_case
    restored_root = tmp_path / "recovered-company"
    restored_path = restored_root / "runtime/company.sqlite3"
    restore_database_copy(company.database_path, restored_path)
    new_catalog = tmp_path / "new-catalog.sqlite3"
    create_catalog(new_catalog, OwnerProfile(str(uuid4()), "接续老板", "synthetic-hash"))
    recovered = Storage(new_catalog)
    assert recovered.list_companies() == ()
    metadata = recovered.register_company(restored_root)
    assert metadata.company_id == company.company_id
    assert metadata.database_path == restored_path
    assert recovered.list_companies()[0].backup_directory == restored_root / "backups"
    with pytest.raises(ContractError) as error:
        storage.register_company(restored_root)
    assert error.value.code == "conflict"
    assert storage.list_companies()[0].database_path == company.database_path


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE system_meta SET database_id='" + "z" * 36 + "'",
        "UPDATE company_profile SET company_id='" + "z" * 36 + "'",
        "UPDATE company_profile SET credit_code='91111111111111111a'",
        "UPDATE company_profile SET start_period='2026-13'",
    ],
)
def test_bad_stored_company_identity_is_storage_corruption(storage_case, change):
    storage, company = storage_case
    with sqlite3.connect(company.database_path) as connection:
        connection.execute(change)
    with pytest.raises(ContractError) as error:
        verify_database(company.database_path)
    assert error.value.code == "storage_corrupt"
    with pytest.raises(ContractError) as error:
        with storage.read_unit(company.company_id):
            pass
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE catalog_meta SET catalog_id='" + "z" * 36 + "'",
        "UPDATE owner_profile SET owner_id='" + "z" * 36 + "'",
        "UPDATE owner_profile SET password_hash=''",
        "DELETE FROM owner_profile",
        "UPDATE company_location SET company_id='" + "z" * 36 + "'",
        "UPDATE company_location SET database_id='" + "z" * 36 + "'",
        "UPDATE company_location SET credit_code='91111111111111111a'",
        "UPDATE company_location SET database_path='relative.sqlite3'",
    ],
)
def test_bad_stored_catalog_identity_is_storage_corruption(storage_case, change):
    storage, _ = storage_case
    with sqlite3.connect(storage.catalog_path) as connection:
        connection.execute(change)
    with pytest.raises(ContractError) as error:
        storage.list_companies()
    assert error.value.code == "storage_corrupt"


def test_company_list_uses_full_pinyin_then_original_text_then_identity(storage_case, tmp_path):
    storage, _ = storage_case
    created = []
    for index, name in enumerate(["中兴", "阿里", "北京", "重庆", "西安", "先", "北京"]):
        created.append(
            storage.create_company(
                CompanySpec(
                    str(uuid4()),
                    str(uuid4()),
                    "96666666666666666" + str(index),
                    name,
                    "2026-01",
                    tmp_path / f"pinyin-{index}",
                )
            )
        )
    locations = storage.list_companies()
    assert [item.legal_name for item in locations] == [
        "阿里",
        "北京",
        "北京",
        "重庆",
        "合成公司甲",
        "先",
        "西安",
        "中兴",
    ]
    assert [item.company_id for item in locations if item.legal_name == "北京"] == sorted(
        item.company_id for item in created if item.legal_name == "北京"
    )
