from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage import _database
from ai_accounting_modular.storage.public import (
    CompanySpec,
    OwnerProfile,
    create_catalog,
    create_company_database,
    restore_database_copy,
    verify_database,
)


def new_company(tmp_path):
    return CompanySpec(
        str(uuid4()),
        str(uuid4()),
        "955555555555555555",
        "新合成公司",
        "2026-01",
        tmp_path / "new-company",
    )


def test_directory_failure_cleans_own_new_database_and_allows_retry(
    storage_case, tmp_path, monkeypatch
):
    storage, original_company = storage_case
    spec = new_company(tmp_path)
    original = storage._catalog

    @contextmanager
    def fail_before_commit(*, writable):
        with original(writable=writable) as catalog:
            yield catalog
            if writable:
                raise OSError("synthetic directory failure")

    monkeypatch.setattr(storage, "_catalog", fail_before_commit)
    with pytest.raises(OSError, match="synthetic directory failure"):
        storage.create_company(spec)
    assert not (spec.company_root / "runtime/company.sqlite3").exists()
    assert [item.company_id for item in storage.list_companies()] == [original_company.company_id]
    monkeypatch.setattr(storage, "_catalog", original)
    assert storage.create_company(spec).company_id == spec.company_id


def test_error_after_directory_commit_keeps_registered_company(storage_case, tmp_path, monkeypatch):
    storage, _ = storage_case
    spec = new_company(tmp_path)
    original = storage._catalog

    @contextmanager
    def fail_after_commit(*, writable):
        with original(writable=writable) as catalog:
            yield catalog
        if writable:
            raise OSError("synthetic reply failure")

    monkeypatch.setattr(storage, "_catalog", fail_after_commit)
    with pytest.raises(OSError, match="synthetic reply failure"):
        storage.create_company(spec)
    assert (
        verify_database(spec.company_root / "runtime/company.sqlite3").company_id == spec.company_id
    )
    assert spec.company_id in {item.company_id for item in storage.list_companies()}


def test_cleanup_keeps_identity_captured_before_publication(storage_case, tmp_path, monkeypatch):
    storage, _ = storage_case
    spec = new_company(tmp_path)
    path = spec.company_root / "runtime/company.sqlite3"
    original_catalog, original_publish = storage._catalog, _database._publish

    def change_published_file(source, destination):
        original_publish(source, destination)
        destination.write_bytes(b"outside replacement before catalog insert")

    @contextmanager
    def fail_before_commit(*, writable):
        with original_catalog(writable=writable) as catalog:
            yield catalog
            if writable:
                raise OSError("synthetic directory failure")

    monkeypatch.setattr(_database, "_publish", change_published_file)
    monkeypatch.setattr(storage, "_catalog", fail_before_commit)
    with pytest.raises(ContractError) as error:
        storage.create_company(spec)
    assert error.value.code == "creation_incomplete"
    assert path.read_bytes() == b"outside replacement before catalog insert"


@pytest.mark.parametrize("change", ["bytes", "file_identity", "outside_sidecar"])
def test_directory_failure_does_not_delete_changed_or_outside_files(
    storage_case, tmp_path, monkeypatch, change
):
    storage, _ = storage_case
    spec = new_company(tmp_path)
    path = spec.company_root / "runtime/company.sqlite3"
    original = storage._catalog

    @contextmanager
    def change_then_fail(*, writable):
        with original(writable=writable) as catalog:
            yield catalog
            if writable:
                if change == "bytes":
                    path.write_bytes(b"foreign contents")
                elif change == "file_identity":
                    replacement = tmp_path / "foreign-file.sqlite3"
                    replacement.write_bytes(path.read_bytes())
                    os.replace(replacement, path)
                else:
                    path.with_name(path.name + "-wal").write_bytes(b"outside sidecar")
                raise OSError("synthetic directory failure")

    monkeypatch.setattr(storage, "_catalog", change_then_fail)
    with pytest.raises(ContractError) as error:
        storage.create_company(spec)
    assert error.value.code == "creation_incomplete"
    assert path.exists()
    if change == "bytes":
        assert path.read_bytes() == b"foreign contents"
    elif change == "outside_sidecar":
        assert path.with_name(path.name + "-wal").read_bytes() == b"outside sidecar"
    assert spec.company_id not in {item.company_id for item in storage.list_companies()}


def test_process_interruption_leaves_explicit_registration_path(storage_case, tmp_path):
    storage, _ = storage_case
    spec = new_company(tmp_path)
    data = asdict(spec)
    data["company_root"] = str(data["company_root"])
    script = f"""
import os
from contextlib import contextmanager
from ai_accounting_modular.storage.public import Storage, CompanySpec
storage = Storage({str(storage.catalog_path)!r})
original = storage._catalog
@contextmanager
def interrupt(*, writable):
    with original(writable=writable) as catalog:
        yield catalog
        if writable:
            os._exit(87)
storage._catalog = interrupt
storage.create_company(CompanySpec(**{data!r}))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=15)
    assert result.returncode == 87, result.stderr
    path = spec.company_root / "runtime/company.sqlite3"
    assert verify_database(path).company_id == spec.company_id
    assert spec.company_id not in {item.company_id for item in storage.list_companies()}
    with pytest.raises(ContractError, match="显式登记") as error:
        storage.create_company(spec)
    assert error.value.code == "target_exists"
    assert storage.register_company(spec.company_root).database_id == spec.database_id
    with storage.read_unit(spec.company_id) as context:
        assert context.head == 0


@pytest.mark.parametrize(
    "operation", ["catalog", "company", "independent_company", "snapshot", "restore"]
)
def test_failure_after_new_target_publish_removes_only_this_target(
    storage_case, tmp_path, monkeypatch, operation
):
    storage, company = storage_case
    original = _database._publish

    def fail_after_publish(source, target):
        original(source, target)
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(_database, "_publish", fail_after_publish)
    target = tmp_path / "new-target.sqlite3"
    with pytest.raises(OSError, match="synthetic publish failure"):
        if operation == "catalog":
            create_catalog(target, OwnerProfile(str(uuid4()), "合成老板", "synthetic-hash"))
        elif operation == "company":
            spec = new_company(tmp_path)
            target = spec.company_root / "runtime/company.sqlite3"
            storage.create_company(spec)
        elif operation == "independent_company":
            spec = new_company(tmp_path)
            target = spec.company_root / "runtime/company.sqlite3"
            create_company_database(spec)
        elif operation == "snapshot":
            with storage.read_unit(company.company_id) as context:
                storage.capture_database(context, target)
        else:
            restore_database_copy(company.database_path, target)
    assert not target.exists()
    assert verify_database(company.database_path).head == 0


def test_publish_race_never_cleans_existing_other_file(storage_case, tmp_path, monkeypatch):
    _, company = storage_case
    target = tmp_path / "other-file.sqlite3"
    original = _database._publish

    def another_file_wins(source, destination):
        destination.write_bytes(b"outside file")
        original(source, destination)

    monkeypatch.setattr(_database, "_publish", another_file_wins)
    with pytest.raises(ContractError) as error:
        restore_database_copy(company.database_path, target)
    assert error.value.code == "target_exists"
    assert target.read_bytes() == b"outside file"
