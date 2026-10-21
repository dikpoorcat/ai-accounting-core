"""The source facade registers complete batches without executing business accounting."""

from contextlib import contextmanager
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, validate_contract
from ai_accounting_modular.records.public import open_standard_bundle
from ai_accounting_modular.storage.public import CompanySpec

from ._working_case import case as case


def request(case, action, **values):
    return {"company_id": case.company, "action": action, **values}


def incoming(case):
    with case.kernel.storage.read_unit(case.company) as read:
        root = case.kernel.storage.company_metadata(read).company_root
    path = root / "originals/unassigned/来源.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes("真实原文\r\n金额100.00".encode())
    return {
        "kind": "file",
        "relative_path": "originals/unassigned/来源.txt",
        "archive_month": "2026-01",
    }


def test_import_archives_complete_sources_and_retries_without_establishing_books(case, monkeypatch):
    file = incoming(case)
    calls, original = [], case.kernel.reading.mark_targets

    def mark(impact, unit):
        calls.append(impact)
        return original(impact, unit)

    monkeypatch.setattr(case.kernel.reading, "mark_targets", mark)
    inputs = [file, {"kind": "owner_statement", "text": "老板说明\r\n实际事项"}]
    result = case.app.accounting_sources(request(case, "import", sources=inputs))
    validate_contract("core.schema.json", "SourcesResult", result)
    assert len(result["sources"]) == 2 and result["missing_source_ids"] == []
    assert {item["state"] for item in result["source_statuses"]} == {"present"}
    assert len(calls) == 1
    assert set(calls[0].source_ids) == {source["source_id"] for source in result["sources"]}
    assert not any(calls[0].changes.values())
    assert not calls[0].current_ranges and not calls[0].posted_ranges
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 1
        assert case.kernel.ledger.latest_period(read) is None
        status = case.kernel.records.document_status(read)
        assert status["state"] == "ready"
    bundle = open_standard_bundle(status["manifest_path"])
    # Unused intake remains registered original material; the final business archive
    # includes sources actually referenced by saved facts, objects, notes or pending work.
    assert not bundle.facts and not bundle.sources
    assert case.app.accounting_sources(request(case, "import", sources=inputs)) == result
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 1
    assert len(calls) == 1


def test_source_archive_change_updates_documents_but_preserves_formal_business(case):
    fact = case.seed()
    before = case.get(fact["business_id"], include=["facts", "results"])["businesses"]
    source = case.app.accounting_sources(request(case, "get", source_ids=[case.source_id]))[
        "sources"
    ][0]
    updated = case.app.accounting_sources(
        request(
            case,
            "import",
            sources=[
                {
                    "kind": "owner_statement",
                    "source_id": case.source_id,
                    "text": source["text"],
                    "archive_month": "2026-02",
                }
            ],
        )
    )
    assert updated["sources"][0]["archive_month"] == "2026-02"
    assert case.get(fact["business_id"], include=["facts", "results"])["businesses"] == before
    status = case.app.accounting_documents({"company_id": case.company, "action": "status"})
    assert status["state"] == "ready"
    bundle = open_standard_bundle(status["manifest_path"])
    assert (
        next(s for s in bundle.sources if s["source_id"] == case.source_id)["archive_month"]
        == "2026-02"
    )


def test_get_find_and_inspect_use_the_selected_company_and_leave_no_write(case, tmp_path):
    empty = case.app.accounting_sources(request(case, "find", limit=1))
    assert empty["page"]["total_count"] == 0 and not empty["page"]["has_more"]
    items = [{"kind": "owner_statement", "text": text} for text in ("一行\n二行\n", "另一份")]
    imported = case.app.accounting_sources(request(case, "import", sources=items))
    first = case.app.accounting_sources(request(case, "find", limit=1))
    last = case.app.accounting_sources(
        request(case, "find", limit=1, cursor=first["page"]["next_cursor"])
    )
    assert first["page"]["total_count"] == 2 and len(last["sources"]) == 1
    source = next(s for s in imported["sources"] if s["text"].startswith("一行"))
    source_id = source["source_id"]
    inspected = case.app.accounting_sources(request(case, "inspect", source_id=source_id, limit=1))
    assert inspected["inspection"]["page"]["filtered_count"] == 2
    tail = case.app.accounting_sources(
        request(
            case,
            "inspect",
            source_id=source_id,
            limit=1,
            cursor=inspected["inspection"]["page"]["next_cursor"],
        )
    )
    assert tail["inspection"]["blocks"][0]["text"] == "二行\n"
    other = str(uuid4())
    case.kernel.storage.create_company(
        CompanySpec(
            other, str(uuid4()), "913100000000000002", "另一合成公司", "2026-01", tmp_path / "other"
        )
    )
    missing = case.app.accounting_sources(
        {"company_id": other, "action": "get", "source_ids": [source_id]}
    )
    assert missing["sources"] == [] and missing["missing_source_ids"] == [source_id]
    assert missing["source_statuses"][0]["state"] == "not_found"
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 1


def test_whole_source_batch_rolls_back_when_a_target_notification_fails(case, monkeypatch):
    def fail(*_args):
        raise ContractError("temporary_failure", "合成读取目标暂时不可写。")

    monkeypatch.setattr(case.kernel.reading, "mark_targets", fail)
    with pytest.raises(ContractError) as failed:
        case.app.accounting_sources(
            request(
                case,
                "import",
                sources=[
                    {"kind": "owner_statement", "text": "甲"},
                    {"kind": "owner_statement", "text": "乙"},
                ],
            )
        )
    assert failed.value.code == "temporary_failure"
    assert case.app.accounting_sources(request(case, "find", limit=10))["sources"] == []
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 0
        assert case.kernel.records.document_target_heads(read) == {}


def test_source_inputs_are_fully_verified_before_the_writer_and_invalid_batch_saves_nothing(
    case, monkeypatch
):
    item = incoming(case)
    writer = False
    original_writer, original_prepare = (
        case.kernel.storage.write_unit,
        case.kernel.records.prepare_sources,
    )

    @contextmanager
    def write(*args, **kwargs):
        nonlocal writer
        writer = True
        try:
            with original_writer(*args, **kwargs) as unit:
                yield unit
        finally:
            writer = False

    @contextmanager
    def prepare(*args, **kwargs):
        assert not writer
        with original_prepare(*args, **kwargs) as prepared:
            yield prepared

    monkeypatch.setattr(case.kernel.storage, "write_unit", write)
    monkeypatch.setattr(case.kernel.records, "prepare_sources", prepare)
    bad = item | {"expected_sha256": "0" * 64}
    with pytest.raises(ContractError) as failed:
        case.app.accounting_sources(
            request(case, "import", sources=[{"kind": "owner_statement", "text": "正确资料"}, bad])
        )
    assert failed.value.code == "source_corrupt"
    assert case.app.accounting_sources(request(case, "find", limit=10))["sources"] == []
    case.app.accounting_sources(request(case, "import", sources=[item]))


def test_document_publication_failure_retains_saved_sources_and_retry_resumes_it(case, monkeypatch):
    original = case.kernel.records.prepare_documents

    def fail(*_args):
        raise ContractError("temporary_failure", "合成磁盘暂时不可写。")

    inputs = [{"kind": "owner_statement", "text": "已保全事实"}]
    with monkeypatch.context() as patch:
        patch.setattr(case.kernel.records, "prepare_documents", fail)
        saved = case.app.accounting_sources(request(case, "import", sources=inputs))
    status = case.app.accounting_documents({"company_id": case.company, "action": "status"})
    assert status["state"] == "pending" and status["errors"]
    assert case.app.accounting_sources(request(case, "import", sources=inputs)) == saved
    assert (
        case.app.accounting_documents({"company_id": case.company, "action": "status"})["state"]
        == "ready"
    )
    assert case.kernel.records.prepare_documents == original
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 1


def test_legal_source_batch_over_nine_mib_is_atomic_and_does_not_use_apply_budget(case):
    imports = [
        {"kind": "owner_statement", "text": f"{number:03d}:" + "x" * 19996} for number in range(480)
    ]
    batch = request(case, "import", sources=imports)
    assert len(canonical_json(batch).encode()) > 9 * 1024 * 1024
    result = case.app.accounting_sources(batch)
    assert len(result["sources"]) == len(result["source_statuses"]) == 480
    assert {source["text"] for source in result["sources"]} == {item["text"] for item in imports}
    with case.kernel.storage.read_unit(case.company) as read:
        assert read.head == 1
        assert case.kernel.ledger.latest_period(read) is None
