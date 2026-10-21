"""Explicit final-material cleanup across Records, Ledger and durable backup tasks."""

from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.files.public import ProductionError
from ai_accounting_modular.host.public import create_kernel
from ai_accounting_modular.records.public import open_standard_bundle
from tests.modular.acceptance._support import http, ready
from tests.modular.acceptance.performance.dataset import fixed_clock
from tests.modular.acceptance.performance.projections import PAGES, parameters

from ._working_case import case as case
from .test_sources import incoming, request


def import_unused(case):
    return case.app.accounting_sources(
        request(
            case,
            "import",
            sources=[incoming(case), {"kind": "owner_statement", "text": "已被最终事实取代的说明"}],
        )
    )["sources"]


def root(case):
    with case.kernel.storage.read_unit(case.company) as read:
        return case.kernel.storage.company_metadata(read).company_root


def discard(case, *source_ids):
    result = case.app.accounting_sources(request(case, "discard", source_ids=list(source_ids)))
    validate_contract("core.schema.json", "SourcesCommandResult", result)
    return result


@pytest.fixture
def clock():
    with fixed_clock({"period_to": "2026-01"}) as selected:
        yield selected


def test_only_selected_unused_sources_exit_without_changing_books_or_final_documents(
    case, clock, monkeypatch
):
    fact = case.seed()
    unused = import_unused(case)
    ids = [source["source_id"] for source in unused]
    path = root(case) / next(
        source["relative_path"] for source in unused if source["kind"] == "file"
    )
    untouched = case.app.accounting_sources(
        request(
            case, "import", sources=[{"kind": "owner_statement", "text": "另有未办理的真实资料"}]
        )
    )["sources"][0]
    before = case.get(fact["business_id"])["businesses"]
    status = case.app.accounting_documents(request(case, "status"))
    manifest = Path(status["manifest_path"]).read_bytes()
    monkeypatch.setattr(case.kernel.dashboard, "now", clock.now)
    queries = {page: parameters(case.company, "2026-01", page) for page in PAGES}
    pages_before = {page: ready(case.kernel, page, query) for page, query in queries.items()}
    with case.kernel.storage.read_unit(case.company) as read:
        head = read.head
    missing = str(uuid4())
    result = discard(case, *ids, missing)
    assert set(result["discarded_source_ids"]) == set(ids)
    assert result["missing_source_ids"] == [missing]
    assert result["file_cleanup"] == {"state": "complete", "issues": []}
    assert not path.exists()
    assert case.get(fact["business_id"])["businesses"] == before
    after = case.app.accounting_documents(request(case, "status"))
    assert after["state"] == "ready" and after["set_id"] == status["set_id"]
    assert Path(after["manifest_path"]).read_bytes() == manifest
    for page, query in queries.items():
        response = http(case.kernel, page, query)
        assert response["http_status"] == 200 and response["body"]["state"] == "ready"
        assert response["body"]["response"] == pages_before[page]
    assert case.kernel.reading.run_pending(case.company) == ()
    assert {
        source["source_id"] for source in open_standard_bundle(after["manifest_path"]).sources
    } == {case.source_id}
    remaining = case.app.accounting_sources(request(case, "find", limit=100))["sources"]
    assert {source["source_id"] for source in remaining} == {case.source_id, untouched["source_id"]}
    retry = discard(case, *ids, missing)
    assert not retry["discarded_source_ids"] and set(retry["missing_source_ids"]) == {*ids, missing}
    assert retry["file_cleanup"]["state"] == "complete"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == head + 1
        assert not case.kernel.records.has_source_discards(read)
        assert not case.kernel.ledger.referenced_source_ids(ids, read)
    # The next real backup verifies without requiring or including retired originals.
    with case.kernel.storage.read_unit(case.company) as read:
        database = read.database_id
    task = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": database}
    )
    completed = case.kernel.files.run(case.company, task["task_id"])
    assert completed["state"] == "succeeded", completed
    with case.kernel.files.verified_download(case.company, task["task_id"]) as download:
        backup = case.app.backup_verifier(download.stream)
    assert not any(
        item["path"] == str(path.relative_to(root(case))).replace("\\", "/")
        for item in backup["files"]
    )


def test_current_reference_rejects_whole_batch_before_any_source_or_file_exits(case):
    case.seed()
    unused = import_unused(case)
    with pytest.raises(ContractError) as error:
        discard(case, case.source_id, *(source["source_id"] for source in unused))
    assert error.value.code == "source_in_use"
    result = case.app.accounting_sources(request(case, "find", limit=100))
    assert len(result["sources"]) == 3
    assert all(
        (root(case) / source["relative_path"]).is_file()
        for source in unused
        if source["kind"] == "file"
    )


@pytest.mark.parametrize("state", ["pending", "failed", "succeeded"])
def test_actual_backup_plan_protects_exact_originals_across_restart_until_delivered(
    case, monkeypatch, state
):
    case.seed()
    unused = import_unused(case)
    file = next(source for source in unused if source["kind"] == "file")
    statement = next(source for source in unused if source["kind"] == "owner_statement")
    with case.kernel.storage.read_unit(case.company) as read:
        database = read.database_id
    task = case.app.accounting_backup(
        {"company_id": case.company, "expected_database_id": database}
    )
    if state == "failed":

        def unavailable(*_):
            raise ProductionError("synthetic_unavailable", "合成备份生产暂停。", retryable=False)

        with monkeypatch.context() as patch:
            patch.setattr(case.kernel.files._producer("backup"), "produce", unavailable)
            assert case.kernel.files.run(case.company, task["task_id"])["state"] == "failed"
    elif state == "succeeded":
        assert case.kernel.files.run(case.company, task["task_id"])["state"] == "succeeded"
    catalog = case.kernel.storage.catalog_path
    case.kernel.close()
    case.kernel = create_kernel(catalog)
    assert discard(case, statement["source_id"])["file_cleanup"]["state"] == "complete"
    if state == "succeeded":
        assert discard(case, file["source_id"])["file_cleanup"]["state"] == "complete"
        assert not (root(case) / file["relative_path"]).exists()
    else:
        with pytest.raises(ContractError) as error:
            discard(case, file["source_id"])
        assert error.value.code == "source_in_use"
        assert (root(case) / file["relative_path"]).is_file()


@pytest.mark.parametrize("failure_phase", ["original", "cleanup_writer"])
def test_post_commit_cleanup_failure_is_truthful_and_same_request_resumes(
    case, monkeypatch, failure_phase
):
    sources = import_unused(case)
    file = next(source for source in sources if source["kind"] == "file")
    original = root(case) / file["relative_path"]
    unlink = Path.unlink

    def unavailable(path, *args, **kwargs):
        if path == original:
            raise OSError("synthetic unavailable volume")
        return unlink(path, *args, **kwargs)

    def unavailable_plans(*_):
        raise ContractError("busy", "合成备份计划暂时不可读。")

    with monkeypatch.context() as patch:
        if failure_phase == "original":
            patch.setattr(Path, "unlink", unavailable)
        else:
            # The discard guard succeeds; only the post-commit fresh writer fails.
            plans = case.kernel.files.retained_input_plans
            plan_calls = 0

            def plans_then_fail(*args):
                nonlocal plan_calls
                plan_calls += 1
                return plans(*args) if plan_calls == 1 else unavailable_plans()

            patch.setattr(case.kernel.files, "retained_input_plans", plans_then_fail)
        result = discard(case, file["source_id"])
    assert result["discarded_source_ids"] == [file["source_id"]]
    assert result["file_cleanup"]["state"] == "pending" and result["file_cleanup"]["issues"]
    assert original.is_file()
    missing = case.app.accounting_sources(request(case, "get", source_ids=[file["source_id"]]))
    assert missing["missing_source_ids"] == [file["source_id"]]
    retry = discard(case, file["source_id"])
    assert retry["discarded_source_ids"] == [] and retry["missing_source_ids"] == [
        file["source_id"]
    ]
    assert retry["file_cleanup"] == {"state": "complete", "issues": []}
    assert not original.exists()


def test_idle_cleanup_uses_no_writer_or_backup_plan_scan(case, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("idle source cleanup opened writer or enumerated backup plans")

    monkeypatch.setattr(case.kernel.storage, "write_unit", forbidden)
    monkeypatch.setattr(case.kernel.files, "retained_input_plans", forbidden)
    assert case.app.finish_source_discards(case.company).completed_marker_ids == ()
