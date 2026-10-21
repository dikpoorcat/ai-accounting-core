"""Strict archive extraction and one full target check preserve restored books."""

import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from ai_accounting_modular.application import _backup_verify, _isolated
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.reading import public as reading
from ai_accounting_modular.storage.public import company_metadata

from ._working_case import case as case
from .test_isolated_targets import request


def package(case, *, seed=True):
    if seed:
        case.seed()
    with case.kernel.storage.read_unit(case.company) as read:
        metadata = case.kernel.storage.company_metadata(read)
    task = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": metadata.database_id}
    )
    done = case.kernel.files.run(case.company, task["task_id"])
    assert done["state"] == "succeeded", done
    return Path(done["path"])


@pytest.mark.parametrize("mode,state", [("check", "checked"), ("run", "created")])
def test_restore_checks_source_for_check_or_real_copied_target_for_run_once(
    case, tmp_path, monkeypatch, mode, state
):
    source = package(case)
    offline = create_offline_application()
    original = offline.backup_verifier.verify_snapshot
    calls = []

    def observed(read):
        calls.append(company_metadata(read).company_root)
        return original(read)

    monkeypatch.setattr(offline.backup_verifier, "verify_snapshot", observed)
    result = offline.accounting_isolated(request("restore", mode, source, tmp_path / "restored"))
    assert result["state"] == state, result
    assert len(calls) == 1
    assert (calls[0].parent == tmp_path) is (mode == "run")
    if mode == "run":
        assert calls[0].name.startswith(".accounting-build-")
    else:
        assert not (tmp_path / "restored").exists()


def test_restore_rechecks_real_destination_bytes_even_if_mtime_and_length_match(
    case, tmp_path, monkeypatch
):
    source = package(case)
    offline = create_offline_application()
    copy = _isolated.copy_file

    def damaged(origin, destination, digest, size):
        copy(origin, destination, digest, size)
        if destination.name == "README.md":
            info = destination.stat()
            raw = destination.read_bytes()
            destination.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            os.utime(destination, ns=(info.st_atime_ns, info.st_mtime_ns))

    monkeypatch.setattr(_isolated, "copy_file", damaged)
    target = tmp_path / "not-delivered"
    result = offline.accounting_isolated(request("restore", "run", source, target))
    assert result["state"] == "failed", result
    assert not target.exists()
    assert not list(tmp_path.glob(".accounting-build-*"))


@pytest.mark.parametrize("change", ["same_inode", "replacement"])
def test_restore_fully_checks_changed_formal_body_after_maintenance(
    case, tmp_path, monkeypatch, change
):
    source = package(case)
    offline = create_offline_application()
    verify = _backup_verify.verify_database
    changes = []

    def change_before_full_verification(path):
        before = path.stat().st_ino
        destination = path if change == "same_inode" else path.with_suffix(".replacement")
        if destination != path:
            shutil.copyfile(path, destination)
        with closing(sqlite3.connect(destination)) as connection:
            connection.execute("UPDATE voucher SET summary='changed after first actual SHA'")
            connection.commit()
        if destination != path:
            os.replace(destination, path)
        assert (path.stat().st_ino == before) is (change == "same_inode")
        changes.append(path)
        return verify(path)

    monkeypatch.setattr(_backup_verify, "verify_database", change_before_full_verification)
    target = tmp_path / "not-delivered"
    result = offline.accounting_isolated(request("restore", "run", source, target))
    assert result["state"] == "failed" and result["error"] == "storage_corrupt", result
    assert len(changes) == 1 and not target.exists()
    assert not list(tmp_path.glob(".accounting-build-*"))


def _table_rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return {
            table: connection.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
            for table in ("read_scope_head", "voucher")
        }


def test_restore_maintenance_cannot_modify_formal_books_and_rolls_back_derived_changes(
    case, tmp_path, monkeypatch
):
    case.seed()
    assert case.close(case.check())["state"] == "closed"
    case.kernel.reading.page("brief", reading.MonthScope(case.company, "2026-01"), {})
    source = package(case, seed=False)
    source_bytes = source.read_bytes()
    offline = create_offline_application()
    reset = reading.reset_for_restore
    stage = _isolated._stage
    before, after = [], []

    def incorrect_maintenance(unit):
        repository = unit.repository("reading")
        rows = repository._session.fetchall("SELECT * FROM read_scope_head ORDER BY rowid")
        assert rows
        before.append(
            {
                "read_scope_head": [tuple(row) for row in rows],
                "voucher": [
                    tuple(row)
                    for row in repository._session.fetchall("SELECT * FROM voucher ORDER BY rowid")
                ],
            }
        )
        reset(unit)
        assert not repository._session.fetchall("SELECT * FROM read_scope_head")
        repository._session.execute("UPDATE voucher SET summary='invalid maintenance write'")

    @contextmanager
    def observed_stage(target, check_only):
        with stage(target, check_only) as root:
            try:
                yield root
            finally:
                after.append(_table_rows(root / "runtime/company.sqlite3"))

    monkeypatch.setattr(reading, "reset_for_restore", incorrect_maintenance)
    monkeypatch.setattr(_isolated, "_stage", observed_stage)
    target = tmp_path / "not-delivered"
    result = offline.accounting_isolated(request("restore", "run", source, target))
    assert result["state"] == "failed" and result["error"] == "invalid_operation", result
    assert after[0] == before[0]
    assert after[0]["voucher"]
    assert all("invalid maintenance write" not in row for row in after[0]["voucher"])
    assert source.read_bytes() == source_bytes
    assert not target.exists() and not list(tmp_path.glob(".accounting-build-*"))
