"""The real Files task machine resumes and downloads the real backup producer."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from ai_accounting_modular.application._backup_producer import create_backup_producer
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files.public import (
    ProductionError,
    create_files,
)
from ai_accounting_modular.files.public import (
    repository_factory as files_repository,
)
from ai_accounting_modular.records.public import repository_factory as records_repository
from ai_accounting_modular.storage.public import Storage

from .test_backup_producer import backup_case as backup_case
from .test_backup_producer import byte_verifier


def _service(case):
    storage = Storage(
        case.app.storage.catalog_path,
        repositories=(files_repository(), records_repository()),
    )
    producer = create_backup_producer(
        storage=storage, records=case.app.records, verify_package=byte_verifier
    )
    return create_files(storage, {"backup": producer}), producer


def test_restart_restores_original_lease_and_successful_download_uses_no_capture(backup_case):
    case = backup_case
    files, _ = _service(case)
    plan, context = case.capture()
    receipt = files.ensure_current(plan, plan["scope_head"], str(uuid4()))
    assert receipt["state"] == "pending"
    files.close()
    files, _ = _service(case)
    try:
        files.recover(case.company)
        completed = files.run(case.company, receipt["task_id"])
        assert completed["state"] == "succeeded", completed
        assert not context.capture_root.exists()
        case.advance("200000")
        with files.verified_download(case.company, receipt["task_id"]) as download:
            manifest = byte_verifier(download.stream)
            assert manifest["database_head"] == plan["manifest"]["database_head"]
            assert download.sha256 == completed["sha256"]
            assert download.byte_size == completed["byte_size"]
    finally:
        files.close()


def test_installed_package_followed_by_task_failure_reuses_bytes_and_preserves_previous(
    backup_case, monkeypatch
):
    case = backup_case
    files, producer = _service(case)
    try:
        a, a_context = case.capture()
        first = files.ensure_current(a, a["scope_head"], str(uuid4()))
        first = files.run(case.company, first["task_id"])
        assert first["state"] == "succeeded", first
        old_bytes = (
            a_context.company.backup_directory / producer.describe(a).file_name
        ).read_bytes()
        case.advance("200000")
        b, context = case.capture()
        second = files.ensure_current(b, b["scope_head"], str(uuid4()))
        original = producer.publish

        def fail_after_install(*args):
            original(*args)
            raise ProductionError("lost_confirmation", "合成中断发生在包已安装以后。")

        with monkeypatch.context() as patch:
            patch.setattr(producer, "publish", fail_after_install)
            interrupted = files.run(case.company, second["task_id"])
        assert interrupted["state"] == "pending", interrupted
        current = context.company.backup_directory / producer.describe(b).file_name
        previous = current.with_name(context.company.credit_code + ".previous.finance-company.zip")
        installed = current.read_bytes()
        assert previous.read_bytes() == old_bytes
        completed = files.run(case.company, second["task_id"])
        assert completed["state"] == "succeeded", completed
        assert current.read_bytes() == installed
        assert previous.read_bytes() == old_bytes
        assert not context.capture_root.exists()
    finally:
        files.close()


def test_missing_pending_capture_fails_instead_of_substituting_new_business(backup_case):
    case = backup_case
    files, _ = _service(case)
    plan, context = case.capture()
    receipt = files.ensure_current(plan, plan["scope_head"], str(uuid4()))
    files.close()
    with case.app.storage.read_unit(case.company) as read:
        case.app.records.discard_backup_capture(plan["capture_id"], read)
    assert not context.capture_root.exists()
    case.advance("200000")
    files, _ = _service(case)
    try:
        files.recover(case.company)
        failed = files.get(case.company, receipt["task_id"])
        assert failed["state"] == "failed", failed
        with pytest.raises(ContractError) as problem:
            with files.verified_download(case.company, receipt["task_id"]):
                pytest.fail("a failed capture became downloadable")
        assert problem.value.code == "file_not_ready"
    finally:
        files.close()


def test_slow_old_capture_finishing_after_newer_task_cannot_regress_current_backup(backup_case):
    case = backup_case
    files, _ = _service(case)
    captured, resume = Event(), Event()

    def delayed_request():
        old, context = case.capture()
        with case.producer.recover_lease(old, context):
            captured.set()
            assert resume.wait(30)
            receipt = files.ensure_current(old, old["scope_head"], str(uuid4()))
            return files.run(case.company, receipt["task_id"])

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            delayed = workers.submit(delayed_request)
            assert captured.wait(30)
            case.advance("200000")
            newer, context = case.capture()
            receipt = files.ensure_current(newer, newer["scope_head"], str(uuid4()))
            receipt = files.run(case.company, receipt["task_id"])
            assert receipt["state"] == "succeeded", receipt
            current = context.company.backup_directory / "913100000000000001.finance-company.zip"
            preserved = current.read_bytes()
            resume.set()
            old_receipt = delayed.result(timeout=30)
        assert old_receipt["state"] == "failed", old_receipt
        assert old_receipt["error_code"] == "backup_superseded"
        assert current.read_bytes() == preserved
    finally:
        resume.set()
        files.close()
