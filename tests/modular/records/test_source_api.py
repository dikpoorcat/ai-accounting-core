"""Real records imports and complete metadata pages, isolated by company and source content."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.records.public import RecordsChange, RecordsContext


def store(storage, company, records, imports):
    with ExitStack() as stack:
        with storage.read_unit(company.company_id) as read:
            sources = records.prepare_source_imports(imports, read)
            prepared = stack.enter_context(records.prepare_sources(sources, read))
        with storage.write_unit(company.company_id) as unit:
            plan = records.plan_change(
                RecordsChange(sources=sources),
                RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
            )
            records.apply_current(plan, unit)
    return sources


def incoming(company, name, raw):
    path = company.company_root / "originals/unassigned" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {
        "kind": "file",
        "relative_path": "originals/unassigned/" + name,
        "archive_month": "2026-01",
    }


def query(records, storage, company, **values):
    with storage.read_unit(company.company_id) as read:
        return records.sources({"company_id": company.company_id, **values}, read)


def test_import_generates_identity_hash_reuses_fixed_content_and_registers_atomically(records_case):
    storage, (company, _), records = records_case
    item = incoming(company, "原件.txt", b"actual original\r\n100.00")
    imports = [item, item, {"kind": "owner_statement", "text": "老板原话\r\n实际事项"}]
    sources = store(storage, company, records, imports)
    assert len(sources) == 2
    file, statement = sources
    assert file["relative_path"] == f"originals/2026-01/{file['source_id']}/原件.txt"
    assert file["sha256"] == sha256(b"actual original\r\n100.00").hexdigest()
    assert statement["text"] == "老板原话\n实际事项"
    repeated = store(storage, company, records, imports)
    assert sources == repeated
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
    result = query(
        records, storage, company, action="get", source_ids=[s["source_id"] for s in sources]
    )
    assert len(result["sources"]) == 2
    assert all(item["state"] == "present" for item in result["source_statuses"])


def test_sources_are_company_scoped_and_cannot_escape_originals(records_case, tmp_path):
    storage, (company, other), records = records_case
    source = store(storage, company, records, [{"kind": "owner_statement", "text": "真实原文"}])[0]
    result = query(
        records, storage, other, action="inspect", source_id=source["source_id"], limit=20
    )
    assert result["sources"] == []
    assert result["missing_source_ids"] == [source["source_id"]]
    assert result["source_statuses"][0]["state"] == "not_found"
    with storage.read_unit(other.company_id) as read, pytest.raises(ContractError) as failed:
        records.sources(
            {
                "company_id": company.company_id,
                "action": "get",
                "source_ids": [source["source_id"]],
            },
            read,
        )
    assert failed.value.code == "invalid_input"
    for path in ("../outside.txt", str(tmp_path / "outside.txt"), "originals/../../outside.txt"):
        with storage.read_unit(company.company_id) as read, pytest.raises(ContractError):
            records.prepare_source_imports([{"kind": "file", "relative_path": path}], read)


def test_concurrent_empty_catalog_preparation_has_one_stable_company_content_identity(records_case):
    storage, (company, other), records = records_case
    item = incoming(company, "并行原件.txt", b"same actual bytes")

    def prepare():
        with storage.read_unit(company.company_id) as read:
            return records.prepare_source_imports([item], read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: prepare(), range(2)))
    assert first == second
    # Both preparations happened before either registration, so the reader's empty index
    # cannot be the mechanism that deduplicates these two independent results.
    for sources in (first, second):
        with ExitStack() as stack:
            with storage.read_unit(company.company_id) as read:
                prepared = stack.enter_context(records.prepare_sources(sources, read))
            with storage.write_unit(company.company_id) as unit:
                plan = records.plan_change(
                    RecordsChange(sources=sources),
                    RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
                )
                records.apply_current(plan, unit)
    result = query(records, storage, company, action="find", limit=20)
    assert result["sources"] == list(first)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
    different_company = store(
        storage, other, records, [incoming(other, "并行原件.txt", b"same actual bytes")]
    )[0]
    assert different_company["source_id"] != first[0]["source_id"]


def test_existing_identity_name_and_media_win_over_a_later_extension(records_case):
    storage, (company, _), records = records_case
    explicit = str(uuid4())
    original = store(
        storage,
        company,
        records,
        [incoming(company, "原文.txt", b"description,100.00") | {"source_id": explicit}],
    )[0]
    reused = store(
        storage, company, records, [incoming(company, "另一名称.csv", b"description,100.00")]
    )[0]
    assert reused == original
    assert reused["source_id"] == explicit
    assert reused["media_type"] == "text/plain"
    result = query(records, storage, company, action="inspect", source_id=explicit, limit=20)[
        "inspection"
    ]
    assert result["blocks"][0]["location"].startswith("line:1:")
    # A rebuilt full Source may carry a precise declared media type that filename guessing
    # cannot reproduce. Importing those same fixed bytes must preserve it as well.
    precise = original | {"media_type": "text/x-original-statement"}
    with ExitStack() as stack:
        with storage.read_unit(company.company_id) as read:
            prepared = stack.enter_context(records.prepare_sources([precise], read))
        with storage.write_unit(company.company_id) as unit:
            plan = records.plan_change(
                RecordsChange(sources=(precise,)),
                RecordsContext(unit, str(uuid4()), prepared_sources=prepared),
            )
            records.apply_current(plan, unit)
    assert (
        store(
            storage, company, records, [incoming(company, "又一名称.csv", b"description,100.00")]
        )[0]
        == precise
    )


def test_source_identity_cannot_change_between_statement_and_file(records_case):
    storage, (company, _), records = records_case
    file_import = incoming(company, "说明.txt", b"actual fixed bytes")
    statement_import = {"kind": "owner_statement", "text": "实际说明"}
    file, statement = store(storage, company, records, [file_import, statement_import])
    for item, source in ((file_import, statement), (statement_import, file)):
        with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as failed:
            records.prepare_source_imports([item | {"source_id": source["source_id"]}], read)
        assert failed.value.code == "conflict"


@pytest.mark.parametrize("length", [224, 255])
def test_long_original_names_remain_complete_without_uuid_prefix_overflow(records_case, length):
    storage, (company, _), records = records_case
    name = "a" * (length - 4) + ".txt"
    raw = b"original file with an intact human name"
    source = store(storage, company, records, [incoming(company, name, raw)])[0]
    assert source["original_name"] == name
    assert source["relative_path"] == f"originals/2026-01/{source['source_id']}/{name}"
    assert (company.company_root / source["relative_path"]).read_bytes() == raw


@pytest.mark.parametrize(
    "name", ["../outside.txt", "sub/file.txt", "a\\b.txt", "a:b.txt", ".", ".."]
)
def test_explicit_source_names_are_single_safe_components(records_case, name):
    storage, (company, _), records = records_case
    source = store(storage, company, records, [incoming(company, "原文.txt", b"actual")])[0]
    source_id = str(uuid4())
    changed = source | {
        "source_id": source_id,
        "original_name": name,
        "relative_path": f"originals/2026-01/{source_id}/{name}",
    }
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as failed:
        with records.prepare_sources([changed], read):
            pass
    assert failed.value.code == "invalid_path"
    assert repr(name) in failed.value.message


def test_unregistered_published_source_is_not_overwritten_when_bytes_have_changed(records_case):
    storage, (company, _), records = records_case
    item = incoming(company, "保全原件.txt", b"correct bytes")
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source_imports([item], read)[0]
    target = company.company_root / source["relative_path"]
    target.write_bytes(b"changed after publication")
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as failed:
        records.prepare_source_imports([item], read)
    assert failed.value.code == "source_corrupt"
    assert target.read_bytes() == b"changed after publication"


@pytest.mark.skipif(os.name != "nt", reason="Windows实际文件名规则")
@pytest.mark.parametrize("name", ["CON.txt", "原件.", "原件 ", "原件?.txt"])
def test_unsupported_windows_original_names_have_explicit_locatable_errors(records_case, name):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read, pytest.raises(ContractError) as failed:
        records.prepare_source_imports(
            [{"kind": "file", "relative_path": "originals/unassigned/" + name}], read
        )
    assert failed.value.code == "invalid_path"
    assert repr(name) in failed.value.message


def test_auto_identity_uses_company_identity_rather_than_root_or_database(records_case, tmp_path):
    from ai_accounting_modular.records.public import create_records, repository_factory
    from ai_accounting_modular.storage.public import (
        CompanySpec,
        OwnerProfile,
        Storage,
        create_catalog,
    )

    storage, (company, _), records = records_case
    item = {"kind": "owner_statement", "text": "同一家公司保全的事实"}
    first = store(storage, company, records, [item])[0]
    catalog = tmp_path / "separate-catalog.sqlite3"
    create_catalog(catalog, OwnerProfile(str(uuid4()), "合成老板", "synthetic-hash"))
    restored = Storage(catalog, repositories=(repository_factory(),))
    same_company = restored.create_company(
        CompanySpec(
            company.company_id,
            str(uuid4()),
            company.credit_code,
            company.legal_name,
            "2026-01",
            tmp_path / "same-company-other-location",
        )
    )
    same_records = create_records(lambda read: restored.company_metadata(read).company_root)
    assert store(restored, same_company, same_records, [item])[0] == first


def test_find_has_complete_501_scope_and_only_stats_returned_sources(records_case, monkeypatch):
    storage, (company, _), records = records_case
    imports = [
        {"kind": "owner_statement", "text": "合成原文" + str(i), "archive_month": "2026-01"}
        for i in range(501)
    ]
    sources = (
        *store(storage, company, records, imports[:500]),
        *store(storage, company, records, imports[500:]),
    )
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("列表不能读取原件字节"))
        first = query(
            records,
            storage,
            company,
            action="find",
            period_from="2026-01",
            period_to="2026-01",
            limit=500,
        )
        last = query(
            records,
            storage,
            company,
            action="find",
            period_from="2026-01",
            period_to="2026-01",
            limit=500,
            cursor=first["page"]["next_cursor"],
        )
    assert first["page"]["total_count"] == first["page"]["filtered_count"] == 501
    assert len(first["sources"]) == 500 and len(last["sources"]) == 1
    assert not last["page"]["has_more"]
    assert [item["source_id"] for item in first["sources"] + last["sources"]] == sorted(
        source["source_id"] for source in sources
    )


def test_find_cursor_is_scope_bound_but_unrelated_month_does_not_change_it(records_case):
    storage, (company, other), records = records_case
    store(
        storage,
        company,
        records,
        [{"kind": "owner_statement", "text": str(i), "archive_month": "2026-01"} for i in range(2)],
    )
    first = query(
        records,
        storage,
        company,
        action="find",
        period_from="2026-01",
        period_to="2026-01",
        limit=1,
    )
    store(
        storage,
        company,
        records,
        [{"kind": "owner_statement", "text": "别月原文", "archive_month": "2026-02"}],
    )
    current = query(
        records,
        storage,
        company,
        action="find",
        period_from="2026-01",
        period_to="2026-01",
        limit=1,
    )
    assert current["page"]["collection_version"] == first["page"]["collection_version"]
    with pytest.raises(ContractError) as failed:
        query(
            records,
            storage,
            other,
            action="find",
            period_from="2026-01",
            period_to="2026-01",
            limit=1,
            cursor=first["page"]["next_cursor"],
        )
    assert failed.value.code == "snapshot_changed"
    store(
        storage,
        company,
        records,
        [{"kind": "owner_statement", "text": "本月新资料", "archive_month": "2026-01"}],
    )
    with pytest.raises(ContractError) as changed:
        query(
            records,
            storage,
            company,
            action="find",
            period_from="2026-01",
            period_to="2026-01",
            limit=1,
            cursor=first["page"]["next_cursor"],
        )
    assert changed.value.code == "snapshot_changed"


def test_status_observes_size_but_inspect_verifies_full_hash(records_case):
    storage, (company, _), records = records_case
    source = store(storage, company, records, [incoming(company, "原文.txt", b"correct")])[0]
    path = company.company_root / source["relative_path"]
    path.write_bytes(b"changed")
    result = query(records, storage, company, action="get", source_ids=[source["source_id"]])
    assert result["source_statuses"][0]["state"] == "present"
    with pytest.raises(ContractError) as changed:
        query(records, storage, company, action="inspect", source_id=source["source_id"], limit=20)
    assert changed.value.code == "source_corrupt"
    path.unlink()
    result = query(records, storage, company, action="get", source_ids=[source["source_id"]])
    assert result["source_statuses"][0]["state"] == "missing_file"


def test_long_text_and_csv_cells_are_complete_across_real_page_cursors(records_case):
    storage, (company, _), records = records_case
    text = "原文" * 25000 + "\n下一行\n"
    source = store(storage, company, records, [incoming(company, "长文.txt", text.encode())])[0]
    rows, cursor = [], None
    while True:
        values = {"action": "inspect", "source_id": source["source_id"], "limit": 1}
        if cursor:
            values["cursor"] = cursor
        result = query(records, storage, company, **values)["inspection"]
        rows.extend(result["blocks"])
        cursor = result["page"]["next_cursor"]
        if not cursor:
            break
    assert "".join(block["text"] for block in rows) == text
    assert all(len(block["text"]) <= 20000 for block in rows)
    assert result["page"]["total_count"] == len(rows)

    long = "真实内容" * 40000
    csv_source = store(
        storage,
        company,
        records,
        [incoming(company, "流水.csv", ("说明,金额\r\n" + long + ",100.00\r\n").encode())],
    )[0]
    result = query(
        records, storage, company, action="inspect", source_id=csv_source["source_id"], limit=500
    )["inspection"]
    assert (
        "".join(
            block["text"] for block in result["blocks"] if block["location"].startswith("CSV!A2")
        )
        == long
    )
    assert any(
        block["location"] == "CSV!B2" and block["text"] == "100.00" for block in result["blocks"]
    )


def test_unknown_media_is_explicit_and_original_bytes_are_not_a_fabricated_empty_success(
    records_case,
):
    storage, (company, _), records = records_case
    source = store(
        storage, company, records, [incoming(company, "扫描.png", b"\x89PNG\r\n\x1a\nsynthetic")]
    )[0]
    result = query(
        records, storage, company, action="inspect", source_id=source["source_id"], limit=20
    )["inspection"]
    assert result["unsupported_media"]
    assert result["unsupported_sections"] == ["all"]
    assert result["blocks"] == []
