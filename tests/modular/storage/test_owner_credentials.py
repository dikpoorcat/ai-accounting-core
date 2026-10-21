from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage.public import OwnerProfile, Storage, create_catalog


def test_credentials_replace_atomically_without_changing_owner_or_companies(storage_case):
    storage, company = storage_case
    catalog_id = storage.catalog_identity()
    assert str(UUID(catalog_id)) == catalog_id
    owner = storage.owner_profile()
    assert owner.recovery_hash is None
    with storage.read_unit(company.company_id) as read:
        original_head = read.head
    changed = storage.replace_owner_credentials(
        owner, password_hash="new-password-hash", recovery_hash="new-recovery-hash"
    )
    assert changed == replace(
        owner, password_hash="new-password-hash", recovery_hash="new-recovery-hash"
    )
    assert storage.owner_profile() == changed
    assert storage.catalog_identity() == catalog_id
    assert storage.list_companies()[0].database_id == company.database_id
    with storage.read_unit(company.company_id) as read:
        assert read.head == original_head
    with pytest.raises(ContractError) as stale:
        storage.replace_owner_credentials(owner, password_hash="stale", recovery_hash=None)
    assert stale.value.code == "conflict"
    assert storage.owner_profile() == changed
    cleared = storage.replace_owner_credentials(
        changed, password_hash="newest-password-hash", recovery_hash=None
    )
    assert cleared.recovery_hash is None
    assert storage.owner_profile() == cleared


@pytest.mark.parametrize(
    "field,value",
    [
        ("password_hash", ""),
        ("recovery_hash", " "),
        ("password_hash", 5),
        ("recovery_hash", "x" * 4097),
    ],
)
def test_invalid_credential_never_replaces_current_profile(storage_case, field, value):
    storage, _ = storage_case
    owner = storage.owner_profile()
    credentials = {"password_hash": "valid", "recovery_hash": "valid-recovery", field: value}
    with pytest.raises(ContractError) as error:
        storage.replace_owner_credentials(owner, **credentials)
    assert error.value.code == "invalid_input"
    assert storage.owner_profile() == owner


def test_initial_recovery_credential_and_hashes_stay_out_of_profile_repr(tmp_path):
    owner = OwnerProfile(str(uuid4()), "合成老板", "private-password", "private-recovery")
    path = create_catalog(tmp_path / "catalog.sqlite3", owner)
    assert Storage(path).owner_profile() == owner
    assert "private-password" not in repr(owner)
    assert "private-recovery" not in repr(owner)
    assert owner.display_name in repr(owner)
