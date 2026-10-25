"""Actual record files and SQLite snapshots; no synthetic ZIP is a product backup."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_documents import (
    _first_facts,
    _prepare,
    _publish,
    _save_company_note,
)
from test_documents import (
    _publish_company_note as _publish_changed_company_note,
)

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.records.public import RecordsChange, RecordsContext, create_records
from ai_accounting_modular.storage.public import verify_database


def _ready(case):
    fact, obj = _first_facts(case)
    _save_company_note(case)
    prepared = _prepare(case, [fact], [obj])
    assert _publish(case, prepared)["state"] == "ready"
    return prepared.manifest


def _publish_company_note(case):
    return _publish_changed_company_note(case, expected_seq=1)


def _register_file(case, name, data, *, archive_month="2026-01"):
    storage, (company, _), records = case
    relative = "originals/unassigned/" + name
    path = company.company_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "file", "relative_path": relative, "archive_month": archive_month}, read
        )
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    return source


def _plan(storage, company, read, materials):
    database = storage.capture_database(read, materials.capture_root / "runtime/company.sqlite3")
    raw = database.path.read_bytes()
    inputs = list(materials.inputs) + [
        {
            "source_root": "capture",
            "relative_path": "runtime/company.sqlite3",
            "archive_path": "runtime/company.sqlite3",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
        }
    ]
    return {
        "kind": "backup",
        "company_id": company.company_id,
        "database_id": company.database_id,
        "capture_id": materials.capture_id,
        "scope_head": content_digest([database.head, materials.document_set_id]),
        "manifest": {
            "format": "ai-accounting-company-backup/1",
            "company_identity": materials.company_identity,
            "company_id": company.company_id,
            "database_id": company.database_id,
            "database_head": database.head,
            "structure_fingerprint": database.structure_fingerprint,
            "document_set_id": materials.document_set_id,
            "files": [
                {
                    "path": item["archive_path"],
                    "kind": (
                        "database"
                        if item["archive_path"] == "runtime/company.sqlite3"
                        else "original"
                        if item["archive_path"].startswith("originals/")
                        else "standard"
                    ),
                    "sha256": item["sha256"],
                    "byte_size": item["byte_size"],
                }
                for item in inputs
            ],
        },
        "inputs": inputs,
    }


def _capture(case):
    storage, (company, _), records = case
    with storage.read_unit(company.company_id) as read:
        with records.capture_backup_materials(read, str(uuid4())) as materials:
            return materials, _plan(storage, company, read, materials)


def test_same_read_snapshot_copies_mutable_documents_and_allows_new_publication(records_case):
    storage, (company, _), records = records_case
    before = _ready(records_case)
    original_manifest = (company.company_root / "standard/current.json").read_bytes()
    original_readme = (company.company_root / "standard/README.md").read_bytes()
    old_note = next(item for item in before["shards"] if item["key"] == "notes:company")
    with storage.read_unit(company.company_id) as read:
        with records.capture_backup_materials(read, str(uuid4())) as materials:
            assert _publish_company_note(records_case)["state"] == "ready"
            snapshot = storage.capture_database(
                read, materials.capture_root / "runtime/company.sqlite3"
            )
            assert snapshot.head == materials.head == 2
            assert materials.document_set_id == before["set_id"]
            assert (
                materials.capture_root / "standard/current.json"
            ).read_bytes() == original_manifest
            assert (materials.capture_root / "standard/README.md").read_bytes() == original_readme
            assert (company.company_root / old_note["path"]).exists()
            copied = materials.inputs
            copied[0]["sha256"] = "f" * 64
            assert copied != materials.inputs
        assert not (company.company_root / old_note["path"]).exists()
    assert verify_database(company.database_path).head == 3
    assert materials.capture_root.exists()


def test_capture_keeps_all_registered_originals_without_inventing_statement_files(records_case):
    storage, (company, _), records = records_case
    first = _register_file(records_case, "尚待办理.txt", b"unreferenced registered original")
    second = _register_file(records_case, "仍须保全.txt", b"retained original")
    _ready(records_case)
    materials, _ = _capture(records_case)
    by_path = {item["archive_path"]: item for item in materials.inputs}
    assert first["relative_path"] in by_path and second["relative_path"] in by_path
    assert "originals/unassigned/尚待办理.txt" not in by_path
    assert all("owner_statement" not in path for path in by_path)
    assert len(by_path) == len(materials.inputs)
    for path in ("standard/current.json", "standard/README.md"):
        assert by_path[path]["source_root"] == "capture"
    with storage.read_unit(company.company_id) as read:
        assert records.backup_capture_ids(read) == (materials.capture_id,)
        assert records.discard_backup_capture(materials.capture_id, read)
        assert records.discard_backup_capture(materials.capture_id, read)
    assert (company.company_root / first["relative_path"]).exists()
    assert (company.company_root / "standard/current.json").exists()


def test_stale_database_snapshot_cannot_copy_a_newly_published_manifest(records_case):
    storage, (company, _), records = records_case
    _ready(records_case)
    capture_id = str(uuid4())
    with storage.read_unit(company.company_id) as old:
        _publish_company_note(records_case)
        with pytest.raises(ContractError) as error:
            with records.capture_backup_materials(old, capture_id):
                pytest.fail("mixed database and document generations")
        assert error.value.code == "documents_corrupt"
    assert not (company.company_root / "runtime/tasks" / capture_id).exists()


def test_pending_documents_and_write_units_cannot_start_capture(records_case):
    storage, (company, _), records = records_case
    _first_facts(records_case)
    capture_id = str(uuid4())
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as pending:
            with records.capture_backup_materials(read, capture_id):
                pass
        assert pending.value.code == "documents_pending"
    with storage.write_unit(company.company_id) as unit:
        with pytest.raises(ContractError) as writable:
            with records.capture_backup_materials(unit, capture_id):
                pass
        assert writable.value.code == "invalid_operation"
    assert not (company.company_root / "runtime/tasks" / capture_id).exists()


@pytest.mark.parametrize("damage", ["readme", "part"])
def test_corrupt_current_document_bytes_cannot_become_a_capture(records_case, damage):
    storage, (company, _), records = records_case
    manifest = _ready(records_case)
    path = (
        company.company_root / "standard/README.md"
        if damage == "readme"
        else company.company_root / manifest["shards"][0]["path"]
    )
    path.write_bytes(b"externally corrupted")
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as corrupt:
            with records.capture_backup_materials(read, str(uuid4())):
                pass
        assert corrupt.value.code == "documents_corrupt"
        assert records.backup_capture_ids(read) == ()


def test_restore_exact_saved_parts_survives_new_publication_and_release_keeps_capture(records_case):
    storage, (company, _), records = records_case
    original = _ready(records_case)
    materials, plan = _capture(records_case)
    old_notes = next(item for item in original["shards"] if item["key"] == "notes:company")
    restarted = create_records(lambda read: storage.company_metadata(read).company_root)
    with storage.read_unit(company.company_id) as read:
        with restarted.lease_backup_inputs(plan, read):
            _publish_company_note(records_case)
            assert (company.company_root / old_notes["path"]).exists()
    assert not (company.company_root / old_notes["path"]).exists()
    assert materials.capture_root.exists()
    assert loads_json((materials.capture_root / "standard/current.json").read_bytes()) == original


def test_recovery_uses_saved_original_path_and_never_substitutes_latest_location(records_case):
    storage, (company, _), records = records_case
    source = _register_file(records_case, "归月.txt", b"fixed original")
    _ready(records_case)
    _, plan = _capture(records_case)
    with storage.read_unit(company.company_id) as read:
        moved = records.prepare_source(
            {
                "kind": "file",
                "relative_path": source["relative_path"],
                "source_id": source["source_id"],
                "archive_month": "2026-02",
            },
            read,
        )
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(moved,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
    assert (company.company_root / moved["relative_path"]).exists()
    saved = deepcopy(plan)
    with storage.read_unit(company.company_id) as read:
        with records.lease_backup_inputs(plan, read):
            pass
        (company.company_root / source["relative_path"]).unlink()
        with pytest.raises(ContractError) as missing:
            with records.lease_backup_inputs(plan, read):
                pass
        assert missing.value.code == "capture_missing"
    assert plan == saved
    assert (company.company_root / moved["relative_path"]).exists()


@pytest.mark.parametrize("damage", ["missing", "different_size", "directory"])
def test_saved_capture_inputs_fail_without_recapturing(records_case, damage):
    storage, (company, _), records = records_case
    _ready(records_case)
    materials, plan = _capture(records_case)
    path = materials.capture_root / "runtime/company.sqlite3"
    path.unlink()
    if damage == "different_size":
        path.write_bytes(b"changed")
    elif damage == "directory":
        path.mkdir()
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as invalid:
            with records.lease_backup_inputs(plan, read):
                pass
        assert (
            invalid.value.code
            == {
                "missing": "capture_missing",
                "different_size": "capture_invalid",
                "directory": "invalid_path",
            }[damage]
        )
    assert not path.is_file() or path.read_bytes() == b"changed"


def test_cross_company_or_mutable_company_input_cannot_restore_a_lease(records_case):
    storage, (company, other), records = records_case
    _ready(records_case)
    _, plan = _capture(records_case)
    with storage.read_unit(other.company_id) as read:
        with pytest.raises(ContractError) as wrong:
            with records.lease_backup_inputs(plan, read):
                pass
        assert wrong.value.code == "conflict"
    wrong = deepcopy(plan)
    next(item for item in wrong["inputs"] if item["archive_path"] == "standard/current.json")[
        "source_root"
    ] = "company"
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as mutable:
            with records.lease_backup_inputs(wrong, read):
                pass
        assert mutable.value.code == "invalid_path"


def test_capture_directory_identity_gates_enumeration_and_cleanup(records_case):
    storage, (company, other), records = records_case
    _ready(records_case)
    materials, _ = _capture(records_case)
    unknown_id = str(uuid4())
    unknown = company.company_root / "runtime/tasks" / unknown_id
    unknown.mkdir()
    sentinel = unknown / "unrelated.txt"
    sentinel.write_bytes(b"keep")
    with storage.read_unit(company.company_id) as read:
        assert records.backup_capture_ids(read) == (materials.capture_id,)
        assert not records.discard_backup_capture(materials.capture_id, read, keep_capture=True)
        with pytest.raises(ContractError) as unknown_error:
            records.discard_backup_capture(unknown_id, read)
        assert unknown_error.value.code == "capture_invalid"
        marker = materials.capture_root / "capture.json"
        original = loads_json(marker.read_bytes())
        marker.write_text(
            canonical_json(original | {"company_id": other.company_id}), encoding="utf-8"
        )
        assert records.backup_capture_ids(read) == ()
        with pytest.raises(ContractError) as wrong:
            records.discard_backup_capture(materials.capture_id, read)
        assert wrong.value.code == "capture_invalid"
    assert sentinel.read_bytes() == b"keep"


def test_existing_capture_target_is_never_overwritten(records_case):
    storage, (company, _), records = records_case
    _ready(records_case)
    capture_id = str(uuid4())
    root = company.company_root / "runtime/tasks" / capture_id
    root.mkdir(parents=True)
    sentinel = root / "before.txt"
    sentinel.write_bytes(b"do not overwrite")
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as existing:
            with records.capture_backup_materials(read, capture_id):
                pass
        assert existing.value.code == "target_exists"
    assert list(root.iterdir()) == [sentinel]


def test_exception_after_materials_are_handed_out_does_not_delete_capture(records_case):
    storage, (company, _), records = records_case
    _ready(records_case)
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(RuntimeError, match="caller failed after possible acceptance"):
            with records.capture_backup_materials(read, str(uuid4())) as materials:
                raise RuntimeError("caller failed after possible acceptance")
        assert materials.capture_root.exists()
        assert records.backup_capture_ids(read) == (materials.capture_id,)


def test_nested_restore_leases_release_only_their_own_parts(records_case):
    storage, (company, _), records = records_case
    manifest = _ready(records_case)
    _, plan = _capture(records_case)
    old_notes = next(item for item in manifest["shards"] if item["key"] == "notes:company")
    with storage.read_unit(company.company_id) as read:
        with ExitStack() as outer:
            outer.enter_context(records.lease_backup_inputs(plan, read))
            with records.lease_backup_inputs(plan, read):
                _publish_company_note(records_case)
            assert (company.company_root / old_notes["path"]).exists()
        assert not (company.company_root / old_notes["path"]).exists()


def test_restored_input_lease_can_outlive_its_short_database_read(records_case):
    storage, (company, _), records = records_case
    manifest = _ready(records_case)
    materials, plan = _capture(records_case)
    old_notes = next(item for item in manifest["shards"] if item["key"] == "notes:company")
    with ExitStack() as leases:
        with storage.read_unit(company.company_id) as read:
            leases.enter_context(records.lease_backup_inputs(plan, read))
        _publish_company_note(records_case)
        assert (company.company_root / old_notes["path"]).exists()
    assert not (company.company_root / old_notes["path"]).exists()
    assert materials.capture_root.exists()


def test_capture_cleanup_refuses_links_and_preserves_external_files(records_case, tmp_path):
    storage, (company, _), records = records_case
    _ready(records_case)
    materials, _ = _capture(records_case)
    external = tmp_path / "unrelated"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_bytes(b"outside capture")
    try:
        (materials.capture_root / "linked").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 会话不能创建符号链接。")
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as unsafe:
            records.discard_backup_capture(materials.capture_id, read)
        assert unsafe.value.code == "invalid_path"
        assert records.backup_capture_ids(read) == (materials.capture_id,)
        (materials.capture_root / "linked").unlink()
        assert records.discard_backup_capture(materials.capture_id, read)
    assert sentinel.read_bytes() == b"outside capture"


def test_final_directory_cleanup_failure_preserves_identity_for_retry(records_case, monkeypatch):
    storage, (company, _), records = records_case
    _ready(records_case)
    materials, _ = _capture(records_case)
    original = Path.rmdir
    blocked = False

    def locked_once(path):
        nonlocal blocked
        if path == materials.capture_root and not blocked:
            blocked = True
            raise PermissionError("synthetic transient directory lock")
        return original(path)

    monkeypatch.setattr(Path, "rmdir", locked_once)
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(PermissionError):
            records.discard_backup_capture(materials.capture_id, read)
        assert records.backup_capture_ids(read) == (materials.capture_id,)
        assert records.discard_backup_capture(materials.capture_id, read)
        assert records.backup_capture_ids(read) == ()


def test_real_process_loss_before_task_acceptance_leaves_only_identified_capture(records_case):
    storage, (company, _), records = records_case
    _ready(records_case)
    capture_id = str(uuid4())
    script = """
import os
import sys
from ai_accounting_modular.records.public import create_records, repository_factory
from ai_accounting_modular.storage.public import Storage
storage = Storage(sys.argv[1], repositories=(repository_factory(),))
records = create_records(lambda read: storage.company_metadata(read).company_root)
with storage.read_unit(sys.argv[2]) as read:
    with records.capture_backup_materials(read, sys.argv[3]) as captured:
        (captured.capture_root / 'unfinished.txt').write_bytes(b'owned temporary input')
        os._exit(37)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(storage.catalog_path), company.company_id, capture_id],
        capture_output=True,
        timeout=20,
    )
    assert crashed.returncode == 37, crashed.stderr.decode(errors="replace")
    restarted = create_records(lambda read: storage.company_metadata(read).company_root)
    with storage.read_unit(company.company_id) as read:
        assert restarted.backup_capture_ids(read) == (capture_id,)
        assert restarted.discard_backup_capture(capture_id, read)
        assert restarted.backup_capture_ids(read) == ()
        assert restarted.document_status(read)["state"] == "ready"


def test_original_file_pins_are_present_during_normal_document_cleanup(records_case, monkeypatch):
    from ai_accounting_modular.records import _documents

    storage, (company, _), records = records_case
    source = _register_file(records_case, "原件.txt", b"required original")
    _ready(records_case)
    _, plan = _capture(records_case)
    original = _documents.cleanup_parts
    observed = []

    def observe_cleanup(root, manifest, protected=()):
        observed.append(set(protected))
        return original(root, manifest, protected)

    monkeypatch.setattr(_documents, "cleanup_parts", observe_cleanup)
    with storage.read_unit(company.company_id) as read:
        with records.lease_backup_inputs(plan, read):
            _publish_company_note(records_case)
            assert observed and source["relative_path"] in observed[-1]
    assert source["relative_path"] not in observed[-1]
