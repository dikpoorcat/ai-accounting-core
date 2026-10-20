"""Derived initialization cannot consume the next real business document version."""

from copy import deepcopy

import pytest

from ai_accounting_modular.application import _execution
from ai_accounting_modular.application._isolated import _publish_documents, _unit
from ai_accounting_modular.application._submission import Submission
from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.host.public import create_offline_application
from ai_accounting_modular.records.public import open_standard_bundle

from ._working_case import case as case
from .conftest import initial_request
from .test_isolated_targets import request as isolated_request


def metadata(case):
    with case.kernel.storage.read_unit(case.company) as read:
        return case.kernel.storage.company_metadata(read)


def assert_bundle(root, expected_facts, expected_objects, expected_sources):
    bundle = open_standard_bundle(root / "standard/current.json")
    assert {fact["business_id"] for fact in bundle.facts} == set(expected_facts)
    assert {obj["object_id"] for obj in bundle.objects} == set(expected_objects)
    assert {source["source_id"] for source in bundle.sources} == set(expected_sources)
    return bundle


@pytest.mark.parametrize("initialize", ["finish", "backup_capture"])
def test_empty_company_publishes_at_zero_then_first_business_updates_objects_and_sources(
    case, initialize
):
    company = metadata(case)
    if initialize == "finish":
        assert (
            case.app.accounting_documents({"company_id": case.company, "action": "finish"})["state"]
            == "ready"
        )
    else:
        with case.app.capture_backup_snapshot(case.company, company.database_id):
            pass
        task = case.app.accounting_backup(
            {"company_id": case.company, "expected_database_id": company.database_id}
        )
        completed = case.kernel.files.run(case.company, task["task_id"])
        assert completed["state"] == "succeeded", completed
        with case.kernel.files.verified_download(case.company, task["task_id"]) as download:
            manifest = create_offline_application().backup_verifier(download.stream)
            assert manifest["database_head"] == 0
    assert_bundle(company.company_root, (), (), ())
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 0
        assert set(case.kernel.records.document_target_heads(read).values()) == {0}
        case.app.backup_verifier.verify_snapshot(read)
    cost = case.seed()
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert cost in bundle.facts
    assert sorted(item["kind"] for item in bundle.facts) == [
        "opening.package",
        "operations.expense",
    ]
    assert {obj["object_id"] for obj in bundle.objects} == {case.party}
    assert {source["source_id"] for source in bundle.sources} == {case.source_id}
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 1
        case.app.backup_verifier.verify_snapshot(read)


def test_empty_offline_rebuild_initializes_at_zero_and_keeps_first_real_submission_complete(
    case, tmp_path
):
    company = metadata(case)
    assert (
        case.app.accounting_documents({"company_id": case.company, "action": "finish"})["state"]
        == "ready"
    )
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    offline = create_offline_application()
    target = tmp_path / "empty-rebuilt"
    for mode, expected in (("check", "checked"), ("run", "created")):
        result = offline.accounting_isolated(
            isolated_request("rebuild", mode, bundle.manifest_path, target)
        )
        assert result["state"] == expected, result
        if mode == "check":
            assert not target.exists()
    assert_bundle(target, (), (), ())
    with _unit(offline, target) as read:
        assert read.head == 0
        assert set(offline.records.document_target_heads(read).values()) == {0}
        submission = Submission.from_request(initial_request(read.company_id))
        with offline.records.prepare_sources(submission.sources, read) as prepared:
            with _unit(offline, target, "write") as write:
                _execution.execute(offline, submission, write, prepared_sources=prepared)
    _publish_documents(offline, target)
    assert_bundle(
        target,
        (submission.mutations[0]["fact"]["business_id"],),
        (submission.objects[0]["object_id"],),
        (submission.sources[0]["source_id"],),
    )
    with _unit(offline, target) as read:
        assert read.head == 1
        offline.backup_verifier.verify_snapshot(read)


def test_broken_manifest_with_only_some_pending_scopes_repairs_the_whole_current_set(
    case, monkeypatch
):
    first = case.seed()
    second = case.expense("70000", "2026-02")
    assert case.app.accounting_apply(case.request(second))["completion"] == "complete"
    changed = deepcopy(first)
    changed["data"]["amount_fen"] = "90000"
    request = case.request(changed)
    request["mutations"][0].update(op="replace", expected_seq=1)

    def interrupted(snapshot):
        raise OSError("合成文档中断")

    with monkeypatch.context() as patches:
        patches.setattr(case.kernel.records, "prepare_documents", interrupted)
        result = case.app.accounting_apply(request)
    assert result["completion"] == "saved_documents_pending", result
    company = metadata(case)
    (company.company_root / "standard/current.json").write_bytes(b"broken manifest")
    with case.kernel.storage.read_unit(case.company) as read:
        assert case.kernel.records.pending_document_scopes(read) == ("facts:operations:2026-01",)
        before_head = read.head
    repaired = case.app.accounting_documents({"company_id": case.company, "action": "finish"})
    assert repaired["state"] == "ready", repaired
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    current = {item["business_id"]: item for item in bundle.facts}
    assert current[first["business_id"]] == changed
    assert current[second["business_id"]] == second
    assert {obj["object_id"] for obj in bundle.objects} == {case.party}
    assert {item["source_id"] for item in bundle.sources} == {case.source_id}
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == before_head
        case.app.backup_verifier.verify_snapshot(read)


def test_only_explicit_finish_checks_and_repairs_an_unchanged_broken_part(case, monkeypatch):
    first = case.seed()
    company = metadata(case)
    manifest = loads_json((company.company_root / "standard/current.json").read_bytes())
    part = next(item for item in manifest["shards"] if item["key"] == "objects")
    (company.company_root / part["path"]).write_bytes(b"broken objects")
    assert (
        case.app.accounting_documents({"company_id": case.company, "action": "status"})["state"]
        == "ready"
    )

    def no_full_lease(read):
        raise AssertionError("普通增量归档不能核读全部旧分片")

    second = case.expense("70000", "2026-02")
    with monkeypatch.context() as patches:
        patches.setattr(case.kernel.records, "lease_document_set", no_full_lease)
        saved = case.app.accounting_apply(case.request(second))
    assert saved["completion"] == "complete", saved
    formal = case.get(first["business_id"], second["business_id"])
    repaired = case.app.accounting_documents({"company_id": case.company, "action": "finish"})
    assert repaired["state"] == "ready", repaired
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert {item["business_id"] for item in bundle.facts} >= {
        first["business_id"],
        second["business_id"],
    }
    assert {obj["object_id"] for obj in bundle.objects} == {case.party}
    assert case.get(first["business_id"], second["business_id"]) == formal
    with case.kernel.storage.read_unit(case.company) as read:
        case.app.backup_verifier.verify_snapshot(read)
