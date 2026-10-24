"""A sealed v1 ZIP stays exact while current owner checks run on an upgraded copy."""

import sqlite3
import sys
from contextlib import closing
from hashlib import sha256
from io import StringIO
from zipfile import ZipFile

import pytest

from ai_accounting_modular.application import _backup_verify
from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.host.runtime import _upgrade as host_upgrade
from ai_accounting_modular.host.runtime import cli
from ai_accounting_modular.reading.public import MonthScope
from ai_accounting_modular.storage._schema import module_contract
from ai_accounting_modular.storage.public import (
    company_structure_contract,
    upgrade_company_database,
    verify_database,
)

from ._working_case import case as case
from .test_isolated_targets import read_target, request
from .test_restore_proof import package


def _write_archive(path, entries):
    manifest = loads_json(entries["manifest.json"])
    for item in manifest["files"]:
        raw = entries[item["path"]]
        item.update(sha256=sha256(raw).hexdigest(), byte_size=len(raw))
    entries["manifest.json"] = canonical_json(manifest).encode()
    with ZipFile(path, "w") as output:
        for name, raw in entries.items():
            output.writestr(name, raw)


def _v1_backup(case, tmp_path, *, seed=True):
    """Build only synthetic previous storage from its sealed SQL and real current facts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if seed:
        case.seed()
        with case.kernel.storage.read_unit(case.company) as read:
            database_id = read.database_id
        old_task = case.app.accounting_backup(
            {
                "company_id": case.company,
                "expected_database_id": database_id,
            }
        )
        assert old_task["state"] == "pending"
        saved = case.app.accounting_apply(
            case.request(
                notes=[
                    {
                        "scope": "company",
                        "expected_seq": 0,
                        "text": "*原公司说明*\n第二行不变。",
                        "evidence_ids": [case.source_id],
                    }
                ]
            )
        )
        assert saved["completion"] == "complete", saved
        assert case.close(case.check())["state"] == "closed"
        case.kernel.reading.page("brief", MonthScope(case.company, "2026-01"), {})
    archive = package(case, seed=False)
    with ZipFile(archive) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    current, previous = tmp_path / "fixture-current.sqlite3", tmp_path / "fixture-v1.sqlite3"
    current.write_bytes(entries["runtime/company.sqlite3"])
    contract = company_structure_contract(version=1)
    with closing(sqlite3.connect(current)) as original, closing(sqlite3.connect(previous)) as old:
        original.row_factory = sqlite3.Row
        for item in contract["objects"]:
            if item["type"] == "table":
                old.execute(item["sql"])
        for item in contract["objects"]:
            if item["type"] != "table":
                continue
            table = item["name"]
            columns = [row[1] for row in old.execute(f'PRAGMA table_info("{table}")')]
            selected = ",".join(f'"{column}"' for column in columns)
            for row in original.execute(f'SELECT {selected} FROM "{table}"'):
                values = dict(row)
                if table == "system_meta":
                    values.update(schema_version=1, structure_fingerprint=contract["fingerprint"])
                elif table == "module_schema":
                    module = module_contract(contract, values["module_key"])
                    values.update(
                        version=module.version,
                        contract_digest=module.contract_digest,
                        parent_digest=module.parent_digest,
                        sql_fingerprint=module.sql_fingerprint,
                    )
                placeholders = ",".join("?" for _ in columns)
                old.execute(
                    f'INSERT INTO "{table}"({selected}) VALUES({placeholders})',
                    tuple(values[column] for column in columns),
                )
        for item in contract["objects"]:
            if item["type"] != "table":
                old.execute(item["sql"])
        old.commit()
        assert old.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    verify_database(previous, structure_version=1)
    entries["runtime/company.sqlite3"] = previous.read_bytes()
    entries["contracts/company.json"] = (canonical_json(contract) + "\n").encode()
    manifest = loads_json(entries["manifest.json"])
    manifest["structure_fingerprint"] = contract["fingerprint"]
    entries["manifest.json"] = canonical_json(manifest).encode()
    destination = tmp_path / "previous-structure.finance-company.zip"
    _write_archive(destination, entries)
    return destination, entries


DERIVED = ("file_task", "read_scope_head", "read_component", "read_item")


def _saved_rows(database, *, derived=False):
    with closing(sqlite3.connect(database)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        result = {}
        for table in tables:
            if (table in DERIVED) != derived or table in {"system_meta", "module_schema"}:
                continue
            columns = [
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
                if row[1] != "note_format"
            ]
            selected = ",".join(f'"{column}"' for column in columns)
            result[table] = connection.execute(
                f'SELECT {selected} FROM "{table}" ORDER BY rowid'
            ).fetchall()
        return result


@pytest.mark.parametrize("mode, state", [("check", "checked"), ("run", "created")])
def test_previous_zip_checks_on_private_upgrade_and_restores_original_structure(
    case, tmp_path, monkeypatch, mode, state
):
    archive, entries = _v1_backup(case, tmp_path)
    before = archive.read_bytes()
    manifest = loads_json(entries["manifest.json"])
    offline = create_offline_application()
    actual_upgrade = _backup_verify.upgrade_company_database
    upgraded = []

    def observed(path, *, expected_structure_fingerprint):
        assert path.parent.name.startswith("accounting-backup-upgrade-check-")
        upgraded.append(path)
        return actual_upgrade(path, expected_structure_fingerprint=expected_structure_fingerprint)

    monkeypatch.setattr(_backup_verify, "upgrade_company_database", observed)
    target = tmp_path / "restored-v1"
    result = offline.accounting_isolated(request("restore", mode, archive, target))
    assert result["state"] == state, result
    assert any(check["code"] == "structure_upgrade_required" for check in result["checks"])
    assert len(upgraded) == 1 and not upgraded[0].exists()
    assert archive.read_bytes() == before
    if mode == "check":
        assert not target.exists()
        return
    database = target / "runtime/company.sqlite3"
    physical = verify_database(database, structure_version=1)
    assert (physical.company_id, physical.database_id, physical.head) == (
        manifest["company_id"],
        manifest["database_id"],
        manifest["database_head"],
    )
    assert _saved_rows(database) == _saved_rows(tmp_path / "fixture-v1.sqlite3")
    original_derived = _saved_rows(tmp_path / "fixture-v1.sqlite3", derived=True)
    assert original_derived["file_task"] and original_derived["read_scope_head"]
    assert all(not rows for rows in _saved_rows(database, derived=True).values())
    assert (target / "contracts/company.json").read_bytes() == entries["contracts/company.json"]
    with pytest.raises(ContractError) as error:
        verify_database(database)
    assert error.value.code == "structure_upgrade_required"
    upgrade_company_database(
        database, expected_structure_fingerprint=physical.structure_fingerprint
    )
    with read_target(offline, target) as read:
        assert offline.ledger.closed_through(read) == "2026-01"
        note = offline.records.current_notes(("company",), read)[0]
        assert note["text"] == "*原公司说明*\n第二行不变。" and "text_format" not in note
        assert note["seq"] == 1 and note["evidence_ids"] == [case.source_id]
        assert read.repository("files").executable() == ()
        assert (
            offline.backup_verifier.verify_snapshot(read).document_set_id
            == manifest["document_set_id"]
        )


@pytest.mark.parametrize("change", ["boolean_version", "extra_field"])
def test_previous_zip_contract_must_exactly_match_sealed_contract(case, tmp_path, change):
    archive, entries = _v1_backup(case, tmp_path)
    contract = loads_json(entries["contracts/company.json"])
    if change == "boolean_version":
        contract["version"] = True
    else:
        contract["extra"] = "not the sealed contract"
    entries["contracts/company.json"] = canonical_json(contract).encode()
    changed = tmp_path / "changed-contract.zip"
    _write_archive(changed, entries)
    target = tmp_path / "not-created"
    result = create_offline_application().accounting_isolated(
        request("restore", "run", changed, target)
    )
    assert result["state"] == "failed" and result["error"] == "backup_corrupt", result
    assert not target.exists()


def test_previous_zip_semantic_corruption_is_rejected_after_private_upgrade(case, tmp_path):
    archive, entries = _v1_backup(case, tmp_path)
    database = tmp_path / "broken-v1.sqlite3"
    database.write_bytes(entries["runtime/company.sqlite3"])
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE company_note SET note_text='changed without its digest'")
        connection.commit()
    entries["runtime/company.sqlite3"] = database.read_bytes()
    changed = tmp_path / "corrupt-v1.zip"
    _write_archive(changed, entries)
    before = changed.read_bytes()
    target = tmp_path / "not-created"
    result = create_offline_application().accounting_isolated(
        request("restore", "run", changed, target)
    )
    assert result["state"] == "failed" and result["error"] == "storage_corrupt", result
    assert changed.read_bytes() == before and not target.exists()
    assert not list(tmp_path.glob(".accounting-build-*"))


def test_failed_private_upgrade_preserves_original_zip_and_delivers_no_target(
    case, tmp_path, monkeypatch
):
    archive, entries = _v1_backup(case, tmp_path)
    before = archive.read_bytes()
    actual_upgrade = _backup_verify.upgrade_company_database

    def interrupted(path, *, expected_structure_fingerprint):
        actual_upgrade(path, expected_structure_fingerprint=expected_structure_fingerprint)
        raise ContractError("conflict", "合成故障：隔离核验升级后中断。")

    monkeypatch.setattr(_backup_verify, "upgrade_company_database", interrupted)
    target = tmp_path / "not-created"
    result = create_offline_application().accounting_isolated(
        request("restore", "run", archive, target)
    )
    assert result["state"] == "failed" and result["error"] == "conflict", result
    assert archive.read_bytes() == before and not target.exists()
    assert not list(tmp_path.glob(".accounting-build-*"))


def _restore_previous(archive, target):
    result = create_offline_application().accounting_isolated(
        request("restore", "run", archive, target)
    )
    assert result["state"] == "created", result
    return target / "runtime/company.sqlite3"


def test_offline_upgrade_command_preserves_identity_frozen_books_and_retries(
    case, tmp_path, monkeypatch
):
    archive, entries = _v1_backup(case, tmp_path)
    target = tmp_path / "upgrade-target"
    database = _restore_previous(archive, target)
    manifest = loads_json(entries["manifest.json"])
    fingerprint = company_structure_contract(version=1)["fingerprint"]
    before_archive = archive.read_bytes()
    output = StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(
            sys,
            "argv",
            [
                "finance-modular",
                "database-upgrade",
                "--company-directory",
                str(target),
                "--backup",
                str(archive),
                "--expected-structure-fingerprint",
                fingerprint,
            ],
        )
        patch.setattr(sys, "stdout", output)
        cli.main()
    result = loads_json(output.getvalue())
    assert result["status"] == "succeeded", result
    assert (result["company_id"], result["database_id"], result["database_head"]) == (
        manifest["company_id"],
        manifest["database_id"],
        manifest["database_head"],
    )
    assert result["structure_version"] == 2
    assert result["structure_fingerprint"] == company_structure_contract()["fingerprint"]
    assert host_upgrade.run(target, archive, fingerprint) == result
    assert archive.read_bytes() == before_archive
    physical = verify_database(database)
    assert physical.head == manifest["database_head"]
    offline = create_offline_application()
    with read_target(offline, target) as read:
        assert offline.ledger.closed_through(read) == "2026-01"
        assert read.repository("files").current() == ()
        assert offline.records.current_notes(("company",), read)[0]["seq"] == 1
        assert (
            offline.backup_verifier.verify_snapshot(read).document_set_id
            == manifest["document_set_id"]
        )


def test_offline_upgrade_rejects_a_valid_backup_for_a_different_head_without_writing(
    case, tmp_path
):
    archive, _ = _v1_backup(case, tmp_path)
    target = tmp_path / "upgrade-target"
    database = _restore_previous(archive, target)
    before = database.read_bytes()
    changed = case.app.accounting_apply(case.request(case.expense("20000", "2026-02")))
    assert changed["completion"] == "complete", changed
    newer_archive, _ = _v1_backup(case, tmp_path / "newer", seed=False)
    with pytest.raises(ContractError) as error:
        host_upgrade.run(
            target, newer_archive, company_structure_contract(version=1)["fingerprint"]
        )
    assert error.value.code == "conflict"
    assert database.read_bytes() == before
    verify_database(database, structure_version=1)


def test_in_place_upgrade_and_retry_preserve_prepared_reading_and_files_rows(case, tmp_path):
    archive, entries = _v1_backup(case, tmp_path)
    target = tmp_path / "in-place-v1"
    for name, raw in entries.items():
        if name == "manifest.json":
            continue
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    database = target / "runtime/company.sqlite3"
    manifest = loads_json(entries["manifest.json"])
    fingerprint = company_structure_contract(version=1)["fingerprint"]
    before, derived = _saved_rows(database), _saved_rows(database, derived=True)
    assert derived["file_task"] and derived["read_scope_head"]
    for _ in range(2):
        result = host_upgrade.run(target, archive, fingerprint)
        assert (
            result["status"] == "succeeded" and result["database_head"] == manifest["database_head"]
        )
        assert _saved_rows(database) == before
        assert _saved_rows(database, derived=True) == derived
