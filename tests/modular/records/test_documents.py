from __future__ import annotations

import gc
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.records.public import (
    RecordsChange,
    RecordsContext,
    create_records,
    open_standard_bundle,
)


def company_identity(company):
    return {
        "credit_code": company.credit_code,
        "legal_name": company.legal_name,
        "accounting_standard": "small_enterprise",
        "currency": "CNY",
        "start_period": company.start_period,
    }


def _first_facts(records_case):
    storage, (company, _), records = records_case
    party, business = str(uuid4()), str(uuid4())
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "owner_statement", "text": "公司一月欠供应商费用100元"}, read
        )
    fact = {
        "business_id": business,
        "kind": "operations.expense",
        "period": "2026-01",
        "evidence_ids": [source["source_id"]],
        "data": {
            "creditor_id": party,
            "creditor_kind": "supplier",
            "amount_fen": "10000",
            "expense_class": "administration",
        },
    }
    obj = {"object_id": party, "kind": "organization", "name": "合成供应商", "data": {}}
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
        records.replace_source_uses("business", business, fact["evidence_ids"], unit)
        records.mark_document_targets(("objects", "facts:operations:2026-01"), unit)
    return fact, obj


def _prepare(records_case, facts, objects):
    storage, (company, _), records = records_case
    with storage.read_unit(company.company_id) as read:
        snapshot = records.capture_documents(company_identity(company), facts, objects, read)
    return records.prepare_documents(snapshot)


def _publish(records_case, prepared):
    storage, (company, _), records = records_case
    with storage.maintenance_unit(company.company_id) as unit:
        return records.publish_documents(prepared, unit)


def test_empty_documents_then_first_business_remain_complete_without_a_fake_business_version(
    records_case,
):
    storage, (company, _), records = records_case
    with storage.maintenance_unit(company.company_id) as unit:
        records.ensure_document_targets(unit)
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
        assert records.document_status(read)["state"] == "pending"
    assert _publish(records_case, _prepare(records_case, [], []))["state"] == "ready"
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert bundle.facts == bundle.objects == bundle.sources == ()
    with storage.read_unit(company.company_id) as read:
        assert read.head == 0
    fact, obj = _first_facts(records_case)
    assert _publish(records_case, _prepare(records_case, [fact], [obj]))["state"] == "ready"
    bundle = open_standard_bundle(company.company_root / "standard/current.json")
    assert bundle.facts == (fact,)
    assert bundle.objects == (obj,)
    assert tuple(source["source_id"] for source in bundle.sources) == tuple(fact["evidence_ids"])
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_missing_manifest_is_regenerated_at_the_saved_business_version(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    manifest = company.company_root / "standard/current.json"
    manifest.unlink()
    with storage.maintenance_unit(company.company_id) as unit:
        records.ensure_document_targets(unit)
    assert _publish(records_case, _prepare(records_case, [fact], [obj]))["state"] == "ready"
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_final_fact_manifest_is_offline_complete_and_document_publication_does_not_advance_head(
    records_case,
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    prepared = _prepare(records_case, [fact], [obj])
    status = _publish(records_case, prepared)
    assert status["state"] == "ready"
    assert status["manifest_path"] == str(company.company_root / "standard/current.json")
    manifest = loads_json((company.company_root / "standard/current.json").read_bytes())
    assert manifest["company_identity"] == company_identity(company)
    found = []
    for shard in manifest["shards"]:
        raw = (company.company_root / shard["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == shard["sha256"]
        part = loads_json(raw)
        found.extend(part.get("facts", []))
        assert "result_id" not in part
        assert "command_id" not in part
    assert found == [fact]
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        with records.lease_document_set(read) as leased:
            assert leased == manifest


def test_incremental_correction_reuses_unchanged_parts_and_move_month_has_no_duplicate(
    records_case,
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    initial = _prepare(records_case, [fact], [obj])
    _publish(records_case, initial)
    old = {item["key"]: item for item in initial.manifest["shards"]}
    stamps = {
        key: (company.company_root / item["path"]).stat().st_mtime_ns for key, item in old.items()
    }
    new_fact = fact | {"period": "2026-02", "data": fact["data"] | {"amount_fen": "12000"}}
    with storage.write_unit(company.company_id) as unit:
        # Formal owner persistence is covered by application integration.
        records.mark_document_targets(
            ("facts:operations:2026-01", "facts:operations:2026-02"), unit
        )
    moved = _prepare(records_case, [new_fact], None)
    assert {loads_json(raw)["key"] for raw in moved.snapshot.parts_json} == {
        "facts:operations:2026-02"
    }
    _publish(records_case, moved)
    current = {item["key"]: item for item in moved.manifest["shards"]}
    assert "facts:operations:2026-01" not in current
    for key in old.keys() - {"facts:operations:2026-01"}:
        assert current[key] == old[key]
        assert (company.company_root / current[key]["path"]).stat().st_mtime_ns == stamps[key]
    assert not (company.company_root / old["facts:operations:2026-01"]["path"]).exists()
    facts = loads_json(
        (company.company_root / current["facts:operations:2026-02"]["path"]).read_bytes()
    )["facts"]
    assert facts == [new_fact]


def test_older_preparation_cannot_overwrite_new_target(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    old = _prepare(records_case, [fact], [obj])
    with storage.write_unit(company.company_id) as unit:
        source = records.prepare_source(
            {"kind": "owner_statement", "text": "最终正确费用120元"}, unit
        )
        records.apply_current(
            records.plan_change(
                RecordsChange(sources=(source,)), RecordsContext(unit, str(uuid4()))
            ),
            unit,
        )
        records.replace_source_uses("business", fact["business_id"], (source["source_id"],), unit)
        records.mark_document_targets(("facts:operations:2026-01",), unit)
    new = _prepare(
        records_case,
        [
            fact
            | {
                "evidence_ids": [source["source_id"]],
                "data": fact["data"] | {"amount_fen": "12000"},
            }
        ],
        [obj],
    )
    _publish(records_case, new)
    with pytest.raises(ContractError) as error:
        _publish(records_case, old)
    assert error.value.code == "conflict"
    assert (
        loads_json((company.company_root / "standard/current.json").read_bytes())["set_id"]
        == new.manifest["set_id"]
    )


def test_manifest_switch_followed_by_database_failure_can_resume_without_rebooking(
    records_case, monkeypatch
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    prepared = _prepare(records_case, [fact], [obj])
    from ai_accounting_modular.records import _repository

    original = _repository.RecordsRepository.confirm_documents
    monkeypatch.setattr(
        _repository.RecordsRepository,
        "confirm_documents",
        lambda *args: (_ for _ in ()).throw(
            OSError("synthetic database confirmation interruption")
        ),
    )
    with pytest.raises(OSError):
        _publish(records_case, prepared)
    assert (company.company_root / "standard/current.json").exists()
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1
        assert records.document_status(read)["state"] == "pending"
    monkeypatch.setattr(_repository.RecordsRepository, "confirm_documents", original)
    # A restarted application captures again, rather than retaining the old Python plan.
    recaptured = _prepare(records_case, [fact], [obj])
    assert _publish(records_case, recaptured)["state"] == "ready"
    with storage.read_unit(company.company_id) as read:
        assert read.head == 1


def test_current_failure_is_replaced_by_success_and_corrupt_part_never_leases(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    with storage.maintenance_unit(company.company_id) as unit:
        records.record_document_error(
            records.pending_document_scopes(unit),
            "file_busy",
            "归档文件暂时被占用",
            unit,
            expected_heads=records.document_target_heads(unit),
        )
    with storage.read_unit(company.company_id) as read:
        assert records.document_status(read)["errors"]
        assert read.head == 1
    prepared = _prepare(records_case, [fact], [obj])
    assert _publish(records_case, prepared)["errors"] == []
    shard = prepared.manifest["shards"][0]
    (company.company_root / shard["path"]).write_bytes(b"changed")
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as error:
            with records.lease_document_set(read):
                pass
        assert error.value.code == "documents_corrupt"


def test_late_failure_does_not_poison_a_newer_target(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    prepared = _prepare(records_case, [fact], [obj])
    old_heads = {target.key: target.wanted_head for target in prepared.snapshot.targets}
    _publish(records_case, prepared)
    with storage.write_unit(company.company_id) as unit:
        source = records.resolve_sources(fact["evidence_ids"], unit)[0] | {
            "archive_month": "2026-02"
        }
        change = RecordsChange(sources=(source,))
        records.apply_current(records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit)
    with storage.maintenance_unit(company.company_id) as unit:
        records.record_document_error(
            ("sources:" + fact["evidence_ids"][0],),
            "old_failure",
            "迟到的旧任务失败",
            unit,
            expected_heads=old_heads,
        )
    with storage.read_unit(company.company_id) as read:
        assert records.document_status(read)["state"] == "pending"
        assert records.document_status(read)["errors"] == []


def test_recreated_capture_can_confirm_a_switch_after_an_existing_set(records_case, monkeypatch):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    first = _prepare(records_case, [fact], [obj])
    _publish(records_case, first)
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=(
                {
                    "scope": "company",
                    "expected_seq": 0,
                    "text": "公司按项目提供服务",
                    "evidence_ids": [],
                },
            )
        )
        records.apply_current(records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit)
    second = _prepare(records_case, [], None)
    from ai_accounting_modular.records import _repository

    original = _repository.RecordsRepository.confirm_documents

    def interrupted(*args):
        raise OSError("synthetic confirmation interruption after previous complete set")

    monkeypatch.setattr(_repository.RecordsRepository, "confirm_documents", interrupted)
    with pytest.raises(OSError):
        _publish(records_case, second)
    disk = loads_json((company.company_root / "standard/current.json").read_bytes())
    assert disk["set_id"] == second.manifest["set_id"]
    assert disk["set_id"] != first.manifest["set_id"]
    monkeypatch.setattr(_repository.RecordsRepository, "confirm_documents", original)
    rebuilt = _prepare(records_case, [], None)
    assert rebuilt.snapshot.confirmed_set_id == first.manifest["set_id"]
    assert loads_json(rebuilt.snapshot.base_manifest_json)["set_id"] == second.manifest["set_id"]
    assert _publish(records_case, rebuilt)["state"] == "ready"
    with storage.read_unit(company.company_id) as read:
        assert read.head == 2


def _save_company_note(records_case, *, text="初始公司说明", expected_seq=0):
    storage, (company, _), records = records_case
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=(
                {
                    "scope": "company",
                    "expected_seq": expected_seq,
                    "text": text,
                    "evidence_ids": [],
                },
            )
        )
        records.apply_current(records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit)


def _publish_company_note(records_case, *, text="新的公司现实说明", expected_seq=0):
    _save_company_note(records_case, text=text, expected_seq=expected_seq)
    return _publish(records_case, _prepare(records_case, [], None))


def test_nested_set_leases_pin_old_parts_until_last_release_even_on_exception(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _save_company_note(records_case)
    first = _prepare(records_case, [fact], [obj])
    _publish(records_case, first)
    old_notes = next(shard for shard in first.manifest["shards"] if shard["key"] == "notes:company")
    old_path = company.company_root / old_notes["path"]
    old_bytes = old_path.read_bytes()
    with storage.read_unit(company.company_id) as read:
        with records.lease_document_set(read) as outer:
            with pytest.raises(RuntimeError, match="synthetic reader interruption"):
                with records.lease_document_set(read) as inner:
                    assert outer == inner
                    assert _publish_company_note(records_case, expected_seq=1)["state"] == "ready"
                    assert old_path.read_bytes() == old_bytes
                    raise RuntimeError("synthetic reader interruption")
            assert old_path.read_bytes() == old_bytes
        assert not old_path.exists()
    current = loads_json((company.company_root / "standard/current.json").read_bytes())
    for shard in current["shards"]:
        assert (company.company_root / shard["path"]).exists()


def test_a_live_set_lease_allows_other_thread_publication_and_keeps_its_files(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    with ThreadPoolExecutor(max_workers=1) as workers:
        with storage.read_unit(company.company_id) as read:
            with records.lease_document_set(read) as leased:
                assert (
                    workers.submit(_publish_company_note, records_case).result(timeout=10)["state"]
                    == "ready"
                )
                for shard in leased["shards"]:
                    assert (company.company_root / shard["path"]).exists()


def test_document_leases_are_shared_by_public_factory_instances_for_the_same_root(records_case):
    storage, companies, records = records_case
    company = companies[0]
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    _publish_company_note(records_case)
    other = create_records(
        lambda read: storage.company_metadata(read).company_root / "runtime" / ".."
    )
    with storage.read_unit(company.company_id) as read:
        with records.lease_document_set(read) as leased:
            old_notes = next(shard for shard in leased["shards"] if shard["key"] == "notes:company")
            assert (
                _publish_company_note(
                    (storage, companies, other), expected_seq=1, text="更新公司说明"
                )["state"]
                == "ready"
            )
            assert (company.company_root / old_notes["path"]).exists()
        assert not (company.company_root / old_notes["path"]).exists()


@pytest.mark.parametrize("repeat_preparation", [False, True])
def test_lease_release_keeps_a_prepared_but_not_yet_published_part(
    records_case, repeat_preparation
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    with storage.read_unit(company.company_id) as read:
        with records.lease_document_set(read):
            with storage.write_unit(company.company_id) as unit:
                change = RecordsChange(
                    notes=(
                        {
                            "scope": "company",
                            "expected_seq": 0,
                            "text": "已经生成、尚待发布的最终说明",
                            "evidence_ids": [],
                        },
                    )
                )
                records.apply_current(
                    records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit
                )
            prepared = _prepare(records_case, [], None)
            if repeat_preparation:
                first = prepared
                prepared = records.prepare_documents(first.snapshot)
                assert prepared is not first
                assert prepared.manifest == first.manifest
                del first
                gc.collect()
            note = next(
                shard for shard in prepared.manifest["shards"] if shard["key"] == "notes:company"
            )
        assert (company.company_root / note["path"]).exists()
    assert _publish(records_case, prepared)["state"] == "ready"


def test_preparing_one_company_does_not_block_another_company_status(records_case, monkeypatch):
    from ai_accounting_modular.records import _documents

    storage, companies, records = records_case
    company, other_company = companies
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    other_case = storage, tuple(reversed(companies)), records
    other_fact, other_obj = _first_facts(other_case)
    _publish(other_case, _prepare(other_case, [other_fact], [other_obj]))
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=({"scope": "company", "expected_seq": 0, "text": "正在保存", "evidence_ids": []},)
        )
        records.apply_current(records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit)
    with storage.read_unit(company.company_id) as read:
        snapshot = records.capture_documents(company_identity(company), [], None, read)
    other_records = create_records(lambda read: storage.company_metadata(read).company_root)
    entered, release = Event(), Event()
    original = _documents.atomic_write

    def paused_write(root, relative, data):
        if root == company.company_root:
            entered.set()
            assert release.wait(10), "test must release the paused company write"
        return original(root, relative, data)

    def other_status():
        with storage.read_unit(other_company.company_id) as read:
            return other_records.document_status(read)

    monkeypatch.setattr(_documents, "atomic_write", paused_write)
    with ThreadPoolExecutor(max_workers=2) as workers:
        preparation = workers.submit(records.prepare_documents, snapshot)
        try:
            assert entered.wait(5)
            assert workers.submit(other_status).result(timeout=2)["state"] == "ready"
        finally:
            release.set()
        assert _publish(records_case, preparation.result(timeout=5))["state"] == "ready"


def test_preparation_can_reuse_old_leased_content_after_an_intermediate_publication(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=(
                {"scope": "company", "expected_seq": 0, "text": "有效说明A", "evidence_ids": []},
            )
        )
        records.apply_current(records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit)
    first = _prepare(records_case, [fact], [obj])
    _publish(records_case, first)
    with storage.read_unit(company.company_id) as read:
        with records.lease_document_set(read) as leased:
            old = next(shard for shard in leased["shards"] if shard["key"] == "notes:company")
            _publish_company_note(records_case, text="有效说明B", expected_seq=1)
            with storage.write_unit(company.company_id) as unit:
                change = RecordsChange(
                    notes=(
                        {
                            "scope": "company",
                            "expected_seq": 2,
                            "text": "有效说明A",
                            "evidence_ids": [],
                        },
                    )
                )
                records.apply_current(
                    records.plan_change(change, RecordsContext(unit, str(uuid4()))), unit
                )
            prepared = _prepare(records_case, [], None)
            future = next(
                shard for shard in prepared.manifest["shards"] if shard["key"] == "notes:company"
            )
            assert future["path"] == old["path"]
        assert (company.company_root / future["path"]).exists()
    assert _publish(records_case, prepared)["state"] == "ready"


def test_description_and_source_membership_are_published_as_one_complete_set(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    _publish(records_case, _prepare(records_case, [fact], [obj]))
    path = company.company_root / "standard/current.json"
    original = open_standard_bundle(path)
    with storage.read_unit(company.company_id) as read:
        source = records.prepare_source(
            {"kind": "owner_statement", "text": "已确认这笔费用用于培训。"}, read
        )
    scope = "business:" + fact["business_id"]
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(
                RecordsChange(
                    sources=(source,),
                    notes=(
                        {
                            "scope": scope,
                            "text": "员工培训费报销",
                            "evidence_ids": [source["source_id"]],
                            "expected_seq": 0,
                        },
                    ),
                ),
                RecordsContext(unit, str(uuid4())),
            ),
            unit,
        )
    prepared = _prepare(records_case, [], None)
    assert set(prepared.snapshot.scopes) == {"notes:" + scope, "sources:" + source["source_id"]}
    assert set(prepared.snapshot._part_keys) == set(prepared.snapshot.scopes)
    # Preparing immutable parts leaves the previous complete collection visible.
    assert open_standard_bundle(path).set_id == original.set_id
    assert _publish(records_case, prepared)["state"] == "ready"
    current = open_standard_bundle(path)
    assert current.notes[0]["text"] == "员工培训费报销"
    assert source in current.sources and current.facts == original.facts
    with storage.write_unit(company.company_id) as unit:
        records.clear_notes((scope,), str(uuid4()), unit)
    cleared = _prepare(records_case, [], None)
    assert set(cleared.snapshot.scopes) == set(prepared.snapshot.scopes)
    assert cleared.snapshot.parts_json == ()
    assert open_standard_bundle(path).set_id == current.set_id
    assert _publish(records_case, cleared)["state"] == "ready"
    final = open_standard_bundle(path)
    assert final.notes == () and final.sources == original.sources and final.facts == original.facts
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_new_pending_part_after_capture_cannot_be_claimed_or_overwritten_by_old_preparation(
    records_case,
):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    first = _prepare(records_case, [fact], [obj])
    _publish(records_case, first)
    _save_company_note(records_case, text="第一项已确认现实信息")
    pending = _prepare(records_case, [], None)
    with storage.write_unit(company.company_id) as unit:
        change = RecordsChange(
            notes=(
                {
                    "scope": "period:2026-01",
                    "text": "当月已经核对的完整情况",
                    "evidence_ids": [],
                    "expected_seq": 0,
                    "expected_basis_head": "basis",
                },
            )
        )
        records.apply_current(
            records.plan_change(
                change, RecordsContext(unit, str(uuid4()), {"period:2026-01": "basis"})
            ),
            unit,
        )
    with pytest.raises(ContractError) as error:
        _publish(records_case, pending)
    assert error.value.code == "conflict"
    assert (
        open_standard_bundle(company.company_root / "standard/current.json").set_id
        == first.manifest["set_id"]
    )
    with storage.read_unit(company.company_id) as read:
        assert records.document_status(read)["state"] == "pending"
        assert set(records.pending_document_scopes(read)) == {
            "notes:company",
            "notes:period:2026-01",
        }
    complete = _prepare(records_case, [], None)
    assert set(complete.snapshot.scopes) == {"notes:company", "notes:period:2026-01"}
    assert _publish(records_case, complete)["state"] == "ready"
    assert {
        note["scope"]
        for note in open_standard_bundle(company.company_root / "standard/current.json").notes
    } == {"company", "period:2026-01"}
    with storage.read_unit(company.company_id) as read:
        records.verify_snapshot(company_identity(company), [fact], [obj], read)


def test_source_shard_and_manifest_are_stable_when_sqlite_reverses_unordered_reads(records_case):
    storage, (company, _), records = records_case
    fact, obj = _first_facts(records_case)
    with storage.read_unit(company.company_id) as read:
        extra = tuple(
            records.prepare_source({"kind": "owner_statement", "text": text}, read)
            for text in ("第二份必要资料", "第三份必要资料")
        )
    fact = fact | {"evidence_ids": fact["evidence_ids"] + [item["source_id"] for item in extra]}
    with storage.write_unit(company.company_id) as unit:
        records.apply_current(
            records.plan_change(RecordsChange(sources=extra), RecordsContext(unit, str(uuid4()))),
            unit,
        )
        records.replace_source_uses("business", fact["business_id"], fact["evidence_ids"], unit)
        records.mark_document_targets(("facts:operations:2026-01",), unit)
    with storage.read_unit(company.company_id) as read:
        first = records.capture_documents(company_identity(company), [fact], [obj], read)
        # This SQLite diagnostic changes scan direction without changing data or its business head.
        read._connection.execute("PRAGMA reverse_unordered_selects=ON")
        reverse = records.capture_documents(company_identity(company), [fact], [obj], read)
        read._connection.execute("PRAGMA reverse_unordered_selects=OFF")
    assert first.parts_json == reverse.parts_json
    source_parts = [
        loads_json(raw) for raw in first.parts_json if loads_json(raw)["key"].startswith("sources:")
    ]
    assert [source["source_id"] for part in source_parts for source in part["sources"]] == sorted(
        fact["evidence_ids"]
    )
    assert records.prepare_documents(first).manifest == records.prepare_documents(reverse).manifest
