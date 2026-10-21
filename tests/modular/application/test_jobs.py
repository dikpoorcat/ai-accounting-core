"""The task facade observes actual file-machine states without starting work or scanning tasks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest

from ai_accounting_modular.application.public import create_backup_producer
from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.files.public import ExpireTask, ProductionError, create_files
from ai_accounting_modular.storage.public import CompanySpec

from ._working_case import case as case


@contextmanager
def service(case, monkeypatch, produce):
    producer = create_backup_producer(
        storage=case.kernel.storage,
        records=case.kernel.records,
        verify_package=case.app.backup_verifier,
    )
    original = producer.produce
    monkeypatch.setattr(producer, "produce", lambda plan, context: produce(original, plan, context))
    files = create_files(case.kernel.storage, {"backup": producer})
    monkeypatch.setattr(case.app, "files", files)
    try:
        yield files
    finally:
        files.close()


def start(case):
    with case.kernel.storage.read_unit(case.company) as read:
        database_id = read.database_id
    return case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": database_id}
    )


def status(case, task):
    return case.app.accounting_jobs({"company_id": case.company, "task_id": task["task_id"]})


def test_jobs_observes_real_pending_running_and_verified_success_without_business_writes(
    case, monkeypatch, tmp_path
):
    case.seed()
    started, proceed = Event(), Event()

    def produce(original, plan, context):
        started.set()
        assert proceed.wait(5), "测试须释放实际生产者"
        return original(plan, context)

    with service(case, monkeypatch, produce) as files:
        pending = start(case)
        assert pending["state"] == "pending" and not started.is_set()
        assert status(case, pending) == pending
        assert not started.is_set()
        with case.kernel.storage.read_unit(case.company) as read:
            head = read.head
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker = pool.submit(files.run, case.company, pending["task_id"])
            try:
                assert started.wait(5)
                running = status(case, pending)
                assert running["state"] == "running" and running["attempts"] == 1
                assert "path" not in running
            finally:
                proceed.set()
            succeeded = worker.result(timeout=15)
        result = status(case, pending)
        validate_contract("core.schema.json", "TaskReceipt", result)
        assert result == succeeded and result["state"] == "succeeded"
        with files.verified_download(case.company, result["task_id"]) as download:
            raw = download.stream.read()
            assert sha256(raw).hexdigest() == result["sha256"]
            assert len(raw) == result["byte_size"]
        other = str(uuid4())
        case.kernel.storage.create_company(
            CompanySpec(
                other,
                str(uuid4()),
                "913100000000000002",
                "另一合成公司",
                "2026-01",
                tmp_path / "other",
            )
        )
        for company, task_id in ((other, pending["task_id"]), (case.company, str(uuid4()))):
            with pytest.raises(ContractError) as failed:
                case.app.accounting_jobs({"company_id": company, "task_id": task_id})
            assert failed.value.code == "not_found"
        with case.kernel.storage.read_unit(case.company) as read:
            assert read.head == head


def test_failed_and_expired_jobs_remain_explicit_without_retry_or_fake_delivery(case, monkeypatch):
    case.seed()
    attempts = []

    def produce(_original, _plan, _context):
        attempts.append(1)
        raise ProductionError("synthetic_failure", "合成生产失败。", retryable=False)

    with service(case, monkeypatch, produce) as files:
        pending = start(case)
        files.run(case.company, pending["task_id"])
        failed = status(case, pending)
        assert failed["state"] == "failed" and failed["attempts"] == 1
        assert failed["error_code"] == "synthetic_failure"
        assert "path" not in failed and "sha256" not in failed
        assert status(case, pending) == failed and len(attempts) == 1
        expired = files.update(
            pending["task_id"],
            "failed",
            pending["scope_head"],
            ExpireTask(),
            company_id=case.company,
        )
        assert status(case, pending) == expired and expired["state"] == "expired"


def test_jobs_does_not_accept_directory_queries_or_missing_file_service(assembled):
    app, company = assembled
    with pytest.raises(ContractError) as failed:
        app.accounting_jobs({"company_id": company, "task_id": str(uuid4())})
    assert failed.value.code == "unsupported"
    with pytest.raises(ContractError):
        app.accounting_jobs({"company_id": company, "limit": 500})
